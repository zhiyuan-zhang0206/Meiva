# meiva:note
# A background run through a real pty-sessions service and a real bash.  The completion CLI is a small script that records its argv, so the notice is observed rather than asserted on a call; the log, the launch file and the session's end are read from the disk and from the service.
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from meiva.shell import background, log_ring
from meiva.shell.background import BackgroundRun, BackgroundSite
from meiva.shell.background_plan import EXIT_TAIL_BYTES, Notice, notified_line
from meiva.shell.handle import SessionHandle
from meiva.shell.session_env import SessionHost
from meiva.shell.sessions import ShellSessions


def _recording_cli(directory: Path, argv_file: Path) -> Path:
    cli = directory / "fake-meiva"
    cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{argv_file}'\n")
    cli.chmod(0o755)
    return cli


def _failing_cli(directory: Path) -> Path:
    cli = directory / "fake-meiva-fail"
    cli.write_text("#!/bin/sh\nexit 1\n")
    cli.chmod(0o755)
    return cli


def _start(
    shell: ShellSessions,
    host: SessionHost,
    cmd: str,
    *,
    cwd: Path,
    cli: Path,
    log_dir: Path | None = None,
    name: str = "test-bg",
    keep: bool = False,
    notify: str | None = None,
) -> SessionHandle:
    site = BackgroundSite(
        agent_id=shell.agent_id,
        cli=cli,
        log_dir=log_dir if log_dir is not None else cwd / ".shell_logs",
        host=host,
    )
    run = BackgroundRun(cmd=cmd, name=name, cwd=cwd, keep=keep, ttl=120.0, notify=notify)
    return background.start(shell, run, site)


def _poll_until(condition: Callable[[], bool], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not condition():
        time.sleep(0.1)


def _poll_log(path: Path, ready: Callable[[str], bool], seconds: float = 5.0) -> str:
    deadline = time.monotonic() + seconds
    while True:
        text = path.read_text()
        if ready(text) or time.monotonic() >= deadline:
            return text
        time.sleep(0.05)


def _closed(shell: ShellSessions, handle: SessionHandle) -> bool:
    return handle.session_id not in shell.list()


def _assert_completion_notice_argv(argv: list[str], handle: SessionHandle, agent_id: int) -> None:
    assert argv[0] == "agents"
    assert argv[1] == "send"
    assert argv[2] == str(agent_id)
    assert "finished" in argv[3]
    assert "exited with code" not in argv[3]
    assert f"shell:{handle.session_id}" in argv
    assert handle.output_path in argv
    assert "--completion" in argv


@pytest.mark.flaky
def test_a_run_logs_notifies_and_closes_its_session(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    argv_file = tmp_path / "argv.txt"
    cli = _recording_cli(tmp_path, argv_file)

    handle = _start(live_shell, live_host, "echo hello-bg; exit 3", cwd=tmp_path, cli=cli)

    _poll_until(argv_file.exists, 15)
    assert argv_file.exists(), "completion notice never fired"
    log = _poll_log(Path(handle.output_path), lambda text: "[exit 3]" in text)
    assert "hello-bg" in log, log
    assert log.strip().endswith("[exit 3]"), log
    _assert_completion_notice_argv(argv_file.read_text().splitlines(), handle, live_shell.agent_id)
    _poll_until(lambda: _closed(live_shell, handle), 10)
    assert _closed(live_shell, handle)
    assert not Path(handle.output_path).with_suffix(".sh").exists()


@pytest.mark.flaky
def test_a_grandchild_holding_stdout_does_not_postpone_the_notice(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    argv_file = tmp_path / "argv.txt"
    cli = _recording_cli(tmp_path, argv_file)

    held = _start(
        live_shell,
        live_host,
        "( bash -c 'echo started; sleep 20 &' )",
        cwd=tmp_path,
        cli=cli,
        name="test-bg-grandchild",
        notify="always",
    )
    try:
        _poll_until(argv_file.exists, 10)
        assert argv_file.exists(), "notice waited for the grandchild holding stdout"
        held_log = _poll_log(Path(held.output_path), lambda text: "started" in text)
        assert "started" in held_log, held_log
    finally:
        with contextlib.suppress(Exception):
            live_shell.kill(held.session_id)


@pytest.mark.flaky
def test_a_killed_command_still_sends_a_notice_without_a_status(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    argv_file = tmp_path / "argv.txt"
    cli = _recording_cli(tmp_path, argv_file)

    handle = _start(
        live_shell,
        live_host,
        "( bash -c 'kill -KILL $$' )",
        cwd=tmp_path,
        cli=cli,
        name="test-bg-sigkill",
        notify="always",
    )

    _poll_until(argv_file.exists, 15)
    assert argv_file.exists(), "completion notice never fired"
    assert "exited with code" not in argv_file.read_text()
    _poll_until(lambda: _closed(live_shell, handle), 10)
    assert _closed(live_shell, handle)


@pytest.mark.flaky
def test_a_failed_notice_still_closes_the_session(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    # meiva:note
    # Bug B regression (Task #1115): a failed command must close the session even when the completion notice cannot be delivered. A shell that outlives its command is a live session nobody intended to keep. The CLI exits 1: under the old `&& exit` the close was gated on delivery; with the unconditional `; exit $_ec` the session must end regardless.
    handle = _start(
        live_shell,
        live_host,
        "false",
        cwd=tmp_path,
        cli=_failing_cli(tmp_path),
        name="test-bg-fail",
    )

    _poll_until(lambda: _closed(live_shell, handle), 15)
    assert _closed(live_shell, handle)


@pytest.mark.flaky
def test_a_kept_session_survives_its_command(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    argv_file = tmp_path / "argv.txt"
    cli = _recording_cli(tmp_path, argv_file)

    handle = _start(
        live_shell, live_host, "echo done", cwd=tmp_path, cli=cli, name="test-keep", keep=True
    )
    try:
        _poll_until(argv_file.exists, 15)
        assert argv_file.exists(), "completion notice never fired"
        time.sleep(0.5)
        assert handle.session_id in live_shell.list()
    finally:
        live_shell.kill(handle.session_id)


def test_long_paths_survive_shell_startup(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path, unit_home: Path
) -> None:
    (unit_home / ".bash_profile").write_text("sleep 1\n")
    long_dir = tmp_path / ("long-path-" + "a" * 180)
    long_dir.mkdir()
    argv_file = long_dir / "argv.txt"
    cli = _recording_cli(long_dir, argv_file)

    handle = _start(
        live_shell,
        live_host,
        "echo long-bg; exit 3",
        cwd=tmp_path,
        cli=cli,
        log_dir=long_dir,
        name="test-long-bg",
    )

    _poll_until(argv_file.exists, 15)
    assert argv_file.exists(), live_shell.capture(handle.session_id, 30, scrollback=True)
    _assert_completion_notice_argv(argv_file.read_text().splitlines(), handle, live_shell.agent_id)
    log = _poll_log(Path(handle.output_path), lambda text: text.strip().endswith("[exit 3]"))
    assert log.strip() == "long-bg\n[exit 3]", log
    _poll_until(lambda: _closed(live_shell, handle), 10)
    assert _closed(live_shell, handle)
    assert not Path(handle.output_path).with_suffix(".sh").exists()


def test_a_launch_file_that_cannot_be_written_leaves_no_session(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    log_dir = tmp_path / "logs"
    (log_dir / "0_test-write-fail.sh").mkdir(parents=True)

    with pytest.raises(OSError):
        _start(
            live_shell,
            live_host,
            "true",
            cwd=tmp_path,
            cli=_failing_cli(tmp_path),
            log_dir=log_dir,
            name="test-write-fail",
        )

    assert live_shell.list() == {}


def test_a_command_the_line_refuses_leaves_no_session(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="single line"):
        _start(live_shell, live_host, "a\nb", cwd=tmp_path, cli=_failing_cli(tmp_path))

    assert live_shell.list() == {}


def test_a_second_run_gets_the_next_session_id(
    live_shell: ShellSessions, live_host: SessionHost, tmp_path: Path
) -> None:
    cli = _failing_cli(tmp_path)

    first = _start(
        live_shell, live_host, "sleep 30", cwd=tmp_path, cli=cli, name="first", keep=True
    )
    second = _start(
        live_shell, live_host, "sleep 30", cwd=tmp_path, cli=cli, name="second", keep=True
    )

    assert (first.session_id, second.session_id) == (0, 1)
    assert Path(second.output_path) == tmp_path / ".shell_logs" / "1_second.log"
    assert live_shell.list() == {0: "first", 1: "second"}


def test_closing_run_waits_for_its_log_to_catch_up(tmp_path: Path) -> None:
    # meiva:note
    # The session holds the only copy of a closing run's log until the writer drains the pipe, and the session's process group is killed the moment the shell exits. A writer that is still starting up makes the ordering visible on any host: if the shell can return before its writer drains, the log is still missing the command's own lines at that instant, so the read below fails without the wait and succeeds with it.
    slow_bin = tmp_path / "bin"
    slow_bin.mkdir()
    slow_tee = slow_bin / "tee"
    slow_tee.write_text('#!/bin/sh\nsleep 0.3\nexec /usr/bin/tee "$@"\n')
    slow_tee.chmod(0o755)
    fake_cli = tmp_path / "fake-meiva"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    log = tmp_path / "run.log"
    line = notified_line(
        "echo long-bg; exit 3",
        Notice(agent_id=5, label="Background command 'long-bg'", source="shell:1", cli=fake_cli),
        output_path=log,
        keep=False,
    )
    # meiva:note
    # The interpreter is fixed and the line is the one the plan generated, so the subprocess call runs nothing a caller supplied.
    proc = subprocess.Popen(  # noqa: S603
        ["/bin/bash", "-c", line],
        env={**os.environ, "PATH": f"{slow_bin}:{os.environ['PATH']}"},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert proc.wait(timeout=30) == 3
        assert log.read_text() == "long-bg\n[exit 3]\n"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def _seed_logs(directory: Path, count: int, *, first_id: int = 0) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    base = time.time() - 3600
    for i in range(count):
        path = directory / f"{first_id + i}_seed.log"
        path.write_text("old")
        path.with_suffix(".sh").write_text("not started")
        os.utime(path, (base + i, base + i))


def test_allocating_a_path_touches_it_and_prunes_the_ring(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    _seed_logs(logs, log_ring.OUTPUT_KEEP + 5)

    path = background.allocate_output_path(999, "fresh", log_dir=logs, live=frozenset())

    assert path == logs / "999_fresh.log"
    assert path.exists()
    remaining = list(logs.glob("*.log"))
    assert len(remaining) <= log_ring.OUTPUT_KEEP
    assert path in remaining
    assert {p.stem for p in logs.glob("*.sh")} <= {p.stem for p in remaining}


def test_allocating_a_path_never_evicts_the_log_of_a_live_session(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    _seed_logs(logs, log_ring.OUTPUT_KEEP + 5, first_id=100)
    live_log = logs / "7_cron.log"
    live_log.write_text("standing watcher")
    live_launch = live_log.with_suffix(".sh")
    live_launch.write_text("still needed")
    oldest = time.time() - 7200
    os.utime(live_log, (oldest, oldest))

    background.allocate_output_path(999, "fresh", log_dir=logs, live={7})

    assert live_log.exists()
    assert live_launch.exists()


def test_the_exit_marker_wait_reads_a_bounded_tail() -> None:
    assert len("[exit 255]\n") <= EXIT_TAIL_BYTES
