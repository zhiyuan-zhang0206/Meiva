"""`PtySession` in isolation: the teardown claim, the transcript cap and the kill verdict.

No service and no login shell: a pipe stands in for the master, so these pin what a
running service cannot show directly (a member the caller may not signal needs a
simulated refusal, which only works inside the test process).
"""

from __future__ import annotations

import asyncio
import os
import pty
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import psutil
import pytest

from base.clock import SYSTEM_TIMERS
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.ownership import OwnedProcess
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from base.native_process.tests.fake_process_control import FakeProcessControl
from base.paths import meiva_home
from base.sessions.pty import client, protocol
from base.sessions.pty.tests.pty_reaper import pty_reaper as pty_reaper
from base.sessions.pty.tests.pty_service import service_socket
from base.sessions.record import SessionRecord
from services.agent_runner.pty_sessions import session as session_module
from services.agent_runner.pty_sessions.service import PtyService
from services.agent_runner.pty_sessions.session import PtySession

MakeSession = Callable[[str, int], PtySession]
_TTY_LEADER = Path(__file__).with_name("child_tty_leader.py")
_SHELL_WITH_JOB = Path(__file__).with_name("child_shell_with_job.py")
# meiva:note
# A descriptor number no process holds, so closing it fails the way a vanished descriptor does.
_UNOPENED_FD = 1_000_000


@pytest.fixture
def make_session(tmp_path: Path) -> Iterator[MakeSession]:
    """A factory of sessions on a pipe master; every fd is closed afterwards."""
    sessions: list[PtySession] = []

    def make(name: str, log_cap: int) -> PtySession:
        read_end, write_end = os.pipe()
        os.close(write_end)
        record = SessionRecord(pid=os.getpid(), create_time=0.0, cmd="", cwd="", started_at=0.0)
        session = PtySession(
            name,
            os.getpid(),
            read_end,
            120,
            40,
            record,
            tmp_path / f"{name}.out.log",
            log_cap=log_cap,
        )
        sessions.append(session)
        return session

    yield make
    for session in sessions:
        os.close(session.master_fd)
        os.close(session._log_fd)


def test_begin_finish_has_single_winner(make_session: MakeSession) -> None:
    """Concurrent teardown claims must have exactly one winner: a double
    winner would double-close the master fd."""
    session = make_session("meiva-test-win-1", 1024)
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda: results.append(session.begin_finish())) for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1, results
    assert session.dead
    assert not session.begin_finish(), "later claims must lose"


def test_transcript_log_is_capped(make_session: MakeSession, tmp_path: Path) -> None:
    """The byte transcript stops growing at the per-session cap: past the cap
    the service keeps the session but stops appending."""
    session = make_session("meiva-test-logcap-1", 200)
    log_file = tmp_path / "meiva-test-logcap-1.out.log"
    header = log_file.read_bytes()
    assert header.startswith(b"--- meiva session meiva-test-logcap-1 start=")
    assert header.endswith(f" pid={session.pid} ---\n".encode())
    assert session._log_written == len(header)
    session.log_write(b"a" * 60)
    session.log_write(b"b" * 200)
    assert len(log_file.read_bytes()) == 200
    assert log_file.read_bytes() == header + b"a" * 60 + b"b" * (140 - len(header))
    session.log_write(b"c" * 50)
    assert session._log_written == 200
    assert len(log_file.read_bytes()) == 200, "log must not grow past the cap"


def test_a_transcript_write_failure_is_reported_not_swallowed(
    make_session: MakeSession, tmp_path: Path, loguru_records: LogRecords
) -> None:
    session = make_session("meiva-test-logfail-1", 1024)
    read_only = os.open(tmp_path / "read-only", os.O_CREAT | os.O_RDONLY)
    os.close(session._log_fd)
    session._log_fd = read_only

    session.log_write(b"dropped")

    [event] = loguru_records.events("pty_transcript")
    assert event["level"].name == "WARNING"
    assert event["extra"]["name"] == "meiva-test-logfail-1"
    assert event["extra"]["n"] == 1
    assert session._log_write_failures == 1


# meiva:note
# Point a session at a real process: its pid and recorded birth are that process's own.
def _adopt(session: PtySession, process_id: int) -> PtySession:
    identity = OwnedProcess.capture(psutil.Process(process_id))
    session.pid = identity.pid
    session.record = replace(
        session.record,
        pid=identity.pid,
        create_time=identity.birth,
        starttime=identity.starttime,
    )
    return session


@pytest.fixture
def live_session(make_session: MakeSession) -> Iterator[MakeSession]:
    children: list[subprocess.Popen[bytes]] = []

    def make(name: str, log_cap: int) -> PtySession:
        child = subprocess.Popen(["sleep", "300"], start_new_session=True)
        children.append(child)
        return _adopt(make_session(name, log_cap), child.pid)

    try:
        yield make
    finally:
        for child in children:
            child.kill()
            child.wait(timeout=10)


ShellWithJob = Callable[[str], tuple[PtySession, int]]


# meiva:note
# The fixed test-owned script is started in a session of its own; the test reads its stdout.
@pytest.fixture
def shell_with_job(make_session: MakeSession) -> Iterator[ShellWithJob]:
    shells: list[subprocess.Popen[str]] = []

    def make(name: str) -> tuple[PtySession, int]:
        # meiva:note
        # The command is a fixed argv of the interpreter and an in-repo script: no shell, no user input.
        shell = subprocess.Popen(  # noqa: S603
            [sys.executable, str(_SHELL_WITH_JOB)],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        shells.append(shell)
        assert shell.stdout is not None
        job_pid = int(shell.stdout.readline())
        return _adopt(make_session(name, 1024), shell.pid), job_pid

    try:
        yield make
    finally:
        for shell in shells:
            os.killpg(shell.pid, signal.SIGKILL)
            shell.wait(timeout=10)


TtySession = Callable[[str], tuple[PtySession, int]]


# meiva:note
# The fixed test-owned script is started in a session of its own; the test reads its stdout.
@pytest.fixture
def tty_session(tmp_path: Path) -> Iterator[TtySession]:
    """A session whose master is a real pty with a foreground group led by another process."""
    processes: list[subprocess.Popen[str] | subprocess.Popen[bytes]] = []
    sessions: list[PtySession] = []

    def make(name: str) -> tuple[PtySession, int]:
        master, slave = pty.openpty()
        # meiva:note
        # The command is a fixed argv of the interpreter and an in-repo script: no shell, no user input.
        leader = subprocess.Popen(  # noqa: S603
            [sys.executable, str(_TTY_LEADER), str(slave)],
            pass_fds=(slave,),
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        processes.append(leader)
        os.close(slave)
        assert leader.stdout is not None
        assert leader.stdout.readline() == "ready\n"
        shell = subprocess.Popen(["sleep", "300"], start_new_session=True)
        processes.append(shell)
        record = SessionRecord(pid=shell.pid, create_time=0.0, cmd="", cwd="", started_at=0.0)
        session = PtySession(name, shell.pid, master, 120, 40, record, tmp_path / f"{name}.out.log")
        sessions.append(session)
        return _adopt(session, shell.pid), leader.pid

    try:
        yield make
    finally:
        for process in processes:
            process.kill()
            process.wait(timeout=10)
        for session in sessions:
            os.close(session.master_fd)
            os.close(session._log_fd)


def test_a_concrete_group_signal_error_is_not_a_success(live_session: MakeSession) -> None:
    session = live_session("meiva-test-denied", 1024)
    control = FakeProcessControl(groups={session.pid: session.pid, os.getpid(): os.getpgrp()})
    control.refuse(session.pid, PermissionError("known group signal denied"))
    with pytest.raises(PermissionError, match="known group signal denied"):
        session_module.kill_session(
            session,
            graceful=False,
            interrupted=True,
            process_control=control,
            own_pid=os.getpid(),
            timers=SYSTEM_TIMERS,
        )


def test_a_live_shell_cannot_be_reported_closed(live_session: MakeSession) -> None:
    session = live_session("meiva-test-live", 1024)
    control = FakeProcessControl(groups={session.pid: session.pid, os.getpid(): os.getpgrp()})
    with pytest.raises(RuntimeError, match="shell survived"):
        session_module.kill_session(
            session,
            graceful=False,
            interrupted=False,
            process_control=control,
            own_pid=os.getpid(),
            timers=SYSTEM_TIMERS,
        )


def test_unknown_foreground_is_interrupted_without_touching_the_shells_jobs(
    shell_with_job: ShellWithJob, loguru_records: LogRecords
) -> None:
    session, job_pid = shell_with_job("meiva-test-unknown")
    # meiva:note
    # An earlier observation must not survive an unknown foreground.
    session.known_groups = (session.shell,)

    assert session.capture_foreground() is True

    assert session.known_groups == ()
    assert any(
        "foreground target unknown" in message for message in loguru_records.messages("WARNING")
    )
    assert psutil.Process(job_pid).status() != psutil.STATUS_STOPPED


def test_foreground_in_another_session_is_not_adopted(tty_session: TtySession) -> None:
    session, leader_pid = tty_session("meiva-test-wrong-session")
    assert os.tcgetpgrp(session.master_fd) == leader_pid, "the terminal's foreground group"

    assert session.capture_foreground() is True

    assert session.known_groups == ()


def test_master_close_failure_is_visible_after_the_teardown_claim(
    make_session: MakeSession,
) -> None:
    session = make_session("meiva-test-close-error", 1024)
    master = session.master_fd
    session.master_fd = _UNOPENED_FD

    with pytest.raises(OSError):
        session_module.finish(session, lambda _session: None)
    with pytest.raises(OSError):
        session.wait_dead(0)
    session.master_fd = master
    # finish already closed the transcript; keep the factory cleanup valid.
    session._log_fd = os.open(os.devnull, os.O_WRONLY)


def test_a_failed_callback_and_a_failed_close_keep_the_first_failure_and_log_the_second(
    make_session: MakeSession, loguru_records: LogRecords
) -> None:
    session = make_session("meiva-test-two-failures", 1024)
    transcript = session._log_fd
    session._log_fd = _UNOPENED_FD

    def failed_callback(_session: PtySession) -> None:
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        session_module.finish(session, failed_callback)

    with pytest.raises(OSError):
        os.fstat(session.master_fd)
    with pytest.raises(RuntimeError, match="callback failed"):
        session.wait_dead(0)
    assert any(
        "additional teardown failure" in message for message in loguru_records.messages("WARNING")
    )
    session.master_fd = os.open(os.devnull, os.O_RDONLY)
    session._log_fd = transcript


def test_closure_error_releases_the_allocation_fence(live_session: MakeSession) -> None:
    session = live_session("meiva-test-closure-error", 1024)
    control = FakeProcessControl(groups={session.pid: session.pid, os.getpid(): os.getpgrp()})
    control.refuse(session.pid, PermissionError("group signal denied"))
    refusing = PtyService(
        home=meiva_home(), process_control=control, now=float, timers=SYSTEM_TIMERS, pid=os.getpid()
    )
    refusing._sessions[session.name] = session

    with pytest.raises(PermissionError, match="group signal denied"):
        refusing.close_everything(grace_s=0, kill_s=0)

    assert refusing._closing is False


# meiva:note
# A listener on the service's socket that answers every request with one kill verdict.
class _KillAnswerService:
    def __init__(self) -> None:
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(service_socket()))
        self._server.listen(1)
        self._thread = threading.Thread(target=self._answer_once, daemon=True)
        self._thread.start()

    def _answer_once(self) -> None:
        conn, _ = self._server.accept()
        with conn:
            line = protocol.read_line(conn)
            assert line is not None
            request = protocol.decode_object(line)
            assert request["method"] == "kill"
            verdict = {"mode": "forced", "interrupted": True, "survivors": [4242]}
            conn.sendall(protocol.encode(protocol.ok(request["id"], verdict)))

    def close(self) -> None:
        self._thread.join(timeout=5)
        self._server.close()


def test_the_client_reads_survivors_from_a_kill_answer(unit_home: Path) -> None:
    """A finished but interrupted kill reaches the caller as a verdict, survivors named."""
    (unit_home / "run").mkdir(exist_ok=True)
    service = _KillAnswerService()
    try:
        verdict = client.kill("meiva-test-race-2", graceful=False, socket_path=service_socket())
    finally:
        service.close()

    assert verdict == client.KillVerdict("forced", True, (4242,))


# meiva:note
# A session's transcript is opened under the logs of the home the service was given: this process's
# own home gets nothing, the service's home gets the file. The service's loop is a stand-in that
# takes the session's attach and never runs it.
def test_a_session_transcript_is_opened_in_the_logs_of_the_service_home(tmp_path: Path) -> None:
    served_home = tmp_path / "served-home"
    served_home.mkdir()
    service = PtyService(home=served_home, process_control=SYSTEM_PROCESS_CONTROL, now=time.time)
    service._loop = cast(asyncio.AbstractEventLoop, MagicMock())

    created = service._spawn("meiva-test-home-1", str(tmp_path), dict(os.environ), None, None)
    try:
        assert (served_home / "logs" / "meiva-test-home-1.out.log").is_file()
        assert not (meiva_home() / "logs" / "meiva-test-home-1.out.log").exists()
    finally:
        os.kill(created.pid, signal.SIGKILL)
        session_module.finish(created, lambda _ended: None)
