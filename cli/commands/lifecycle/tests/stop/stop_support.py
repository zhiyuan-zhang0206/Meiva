# meiva:note
# Helpers of the stop tests: a private home, the processes a stop must leave alone, real shells, and the
# notices a stop wrote.
#
# Shell sessions live in the pty-sessions service. A test that needs real shells takes
# the `pty_service` fixture (re-exported here) next to `home`: the service runs
# under the same private home, so the stop under test dials it exactly as it dials
# the unit's own. Without the fixture no service listens, which is the state of a
# unit whose service is down. A home with no root holds no application: the stop meets "no
# live owner", as it does on a unit that was never started. A test that needs a root takes
# `scripted_root`, a real control socket under a short home.

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO

import psutil
import psycopg
import pytest

from base.agents.context.clients import DatabaseFactory
from base.native_process import pid_starttime_ticks
from base.native_process.ownership import OwnedProcess
from base.sessions.posixproc import process_group_has_live_members
from base.sessions.pty import closure, protocol
from base.sessions.pty.tests import pty_jobs as jobs
from base.sessions.pty.tests.process_groups import kill_group_if_alive
from base.sessions.pty.tests.pty_reaper import PtyReaper
from base.sessions.pty.tests.pty_service import PtyServiceProcess as PtyServiceProcess
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from base.sessions.pty.tests.pty_shells import output_until
from base.sessions.record import SessionRecord
from base.telemetry import EventPipeline
from cli.commands.lifecycle.stop.choices import StopChoices, StopRequest, StopWindow
from cli.commands.lifecycle.stop.execute import StopEnvironment
from cli.database.runtime import CommandRuntime


class Launcher(Protocol):
    def __call__(
        self,
        name: str,
        *,
        term: Literal["default", "exit", "ignore"] = "default",
        ignore_hangup: bool = False,
        child_armed: Path | None = None,
    ) -> subprocess.Popen[str]: ...


CHILD = Path(__file__).with_name("_child.py")


@pytest.fixture
def home(unit_home: Path) -> Path:
    """The private home of one stop: the invocation's `MEIVA_HOME`, the service's, the ledger's."""
    return unit_home


@pytest.fixture
def stop_runtime(command_runtime: CommandRuntime, home: Path) -> CommandRuntime:
    """The invocation of a stop in `home`: an agent-runner on a platform with no permissions helper,
    whose browser's debugging port nothing answers on (so a stop never records a stranger's tabs)."""
    from base.deploy.tests.containers import free_port

    command_runtime.machine.set(role="agent-runner")
    command_runtime.config.set_field("browser_cdp_port", free_port())
    return replace(command_runtime, home=home, helper=None)


@contextmanager
def launched_processes(home: Path) -> Generator[Launcher, None, None]:
    """Own fixed-source private sessions until their test and native cleanup finish."""
    processes: list[subprocess.Popen[str]] = []

    def create(
        name: str,
        *,
        term: Literal["default", "exit", "ignore"] = "default",
        ignore_hangup: bool = False,
        child_armed: Path | None = None,
    ) -> subprocess.Popen[str]:
        if term not in ("default", "exit", "ignore"):
            raise ValueError("invalid fixture SIGTERM behavior")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(CHILD),
                "session",
                term,
                "ignore" if ignore_hangup else "default",
                str(child_armed) if child_armed is not None else "",
            ],
            cwd=home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        processes.append(proc)
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        SessionRecord(
            proc.pid,
            psutil.Process(proc.pid).create_time(),
            "private-test",
            str(home),
            time.time(),
            pid_starttime_ticks(proc.pid),
            pgid=os.getpgid(proc.pid),
        ).write(home / "run/sessions" / f"{name}.json")
        return proc

    try:
        yield create
    finally:
        for proc in processes:
            # Test fixture cleanup alone may kill the exact private process group it
            # created, after the assertions prove strict stop left it alive.
            kill_group_if_alive(proc)
            proc.wait(timeout=5)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()


@pytest.fixture
def launch(home: Path) -> Iterator[Launcher]:
    with launched_processes(home) as create:
        yield create


# meiva:note
# SIGKILL is delivered asynchronously: `Popen.wait` reaps the leader while the
# kernel is still tearing down the rest of its group, so a one-shot
# `process_group_has_live_members` right after the kill observes a member that
# is already dying and fails a cleanup that worked. Poll the observation the
# session supervisor itself uses; a member that really survived stays in the
# group and still fails at the deadline.
def assert_group_drained(pgid: int, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while process_group_has_live_members(pgid):
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"process group {pgid} still has a live member after {timeout_s:.0f}s"
            )
        time.sleep(0.02)


def busy_session(
    home: Path, name: str, source: str, reaper: PtyReaper, *, ready_line: str = "job-ready"
) -> tuple[psutil.Process, list[psutil.Process]]:
    """Wait for this new job's bare output marker; pin its native identities."""
    shell = jobs.create(name, home, source)
    reaper.track_session(name)
    output_until(name, ready_line)
    running = jobs.live_children(shell)
    assert running, f"the ready job of {name} is no longer running"
    reaper.track(*running)
    return shell, running


def identity_of(process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> OwnedProcess:
    """The exact identity (pid and birth) of a process a test launched."""
    return OwnedProcess.capture(psutil.Process(process.pid))


def closed_session(name: str, left: tuple[tuple[int, str], ...] = ()) -> closure.ClosedSession:
    """A busy session the closure verified gone (its shell is a long-dead identity)."""
    return closure.ClosedSession(name, OwnedProcess(2_000_000_000, 1.0, None), left)


# meiva:note
# Every closure notice the stops wrote to the database, oldest first.
#
# A notice's idempotency claim is written for every notice, delivered to its owner or dropped
# (a terminated or unknown owner), so the claims are the record of what a stop wrote.
def written_notices(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT response_body FROM api_idempotency WHERE path = 'closure-notice' "
            "ORDER BY completed_at, response_body::text"
        )
        bodies = [row[0] for row in cur.fetchall()]
    conn.commit()
    return [body if isinstance(body, dict) else json.loads(body) for body in bodies]


# meiva:note
# One stop as the operator would state it; every choice has a neutral answer here.
def stop_request(
    *,
    keep_infra: bool | None = None,
    preserve: frozenset[str] = frozenset(),
    keep_browser: bool = True,
    keep_shell_sessions: bool = False,
    announce: bool = False,
    teardown_extras: bool = False,
    force: bool = False,
    window: StopWindow | None = None,
    require_confirmation: bool = False,
    timeout: float = 15.0,
) -> StopRequest:
    return StopRequest(
        choices=StopChoices(
            keep_infra=keep_infra,
            preserve_sessions=preserve,
            keep_browser=keep_browser,
            keep_shell_sessions=keep_shell_sessions,
            announce=announce,
            teardown_extras=teardown_extras,
            force=force,
        ),
        window=window or StopWindow(for_seconds=None, reason=None),
        require_confirmation=require_confirmation,
        timeout=timeout,
    )


# meiva:note
# The invocation of a stop: its runtime, its database and pipeline, and its stdin.
def stop_environment(
    runtime: CommandRuntime,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    *,
    stdin: TextIO | None = None,
    shell_cleanup_wait_s: float | None = None,
) -> StopEnvironment:
    environment = StopEnvironment(
        command=runtime,
        database_factory=database_factory,
        producer=producer,
        retained_children=[],
        stdin=stdin,
    )
    if shell_cleanup_wait_s is None:
        return environment
    return replace(environment, shell_cleanup_wait_s=shell_cleanup_wait_s)


# meiva:note
# A pty-sessions service that answers every request with an operational error.
#
# It listens where the real service would, so the stop dials it exactly as it dials the unit's
# own; a closure the real service cannot be made to fail (a denied group signal) fails here.
class RefusingPtyService:
    def __init__(self, message: str) -> None:
        from base.sessions.pty.paths import fallback_dir, service_socket_path

        path = service_socket_path()
        if path.parent == fallback_dir():
            path.parent.mkdir(mode=0o700, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self._path = path
        self._message = message
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                line = protocol.read_line(conn)
                if line is None:
                    continue
                request = protocol.decode_object(line)
                conn.sendall(protocol.encode(protocol.err(request["id"], 1, self._message)))

    def close(self) -> None:
        self._server.close()
        self._thread.join(timeout=5)
        self._path.unlink(missing_ok=True)


@pytest.fixture
def refusing_pty_service(home: Path) -> Iterator[RefusingPtyService]:
    del home
    service = RefusingPtyService("known group signal denied")
    try:
        yield service
    finally:
        service.close()
