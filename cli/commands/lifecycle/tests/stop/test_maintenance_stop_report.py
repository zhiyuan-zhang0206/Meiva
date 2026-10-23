from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

import psutil
import pytest

from base.deploy.lifecycle import status_journal
from base.native_process.ownership import OwnedProcess
from base.sessions.pty import closure
from base.sessions.record import SessionRecord
from cli.commands.data_plane import maintenance_stop_report as report
from cli.commands.lifecycle.stop import report as stop_report
from cli.commands.lifecycle.stop import shell_sessions as shells
from cli.commands.lifecycle.tests.stop.stop_support import (
    Launcher,
    assert_group_drained,
    closed_session,
    identity_of,
    launched_processes,
)
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import launch as launch
from cli.commands.lifecycle.tests.stop.stop_support import stop_runtime as stop_runtime
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from ops import pty_close_notices


def settle(
    outcome: closure.Outcome,
    runtime: CommandRuntime,
    database_factory: OperatorDatabaseFactory,
    *,
    window_s: float,
) -> None:
    shells.settle_closure(
        outcome,
        pty_close_notices.STOP_REASON,
        deadline=time.monotonic() + window_s,
        direct_db=False,
        database_factory=database_factory,
        command=runtime,
    )


def test_shell_survivor_names_itself_and_persists_exact_inventory(
    home: Path,
    launch: Launcher,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    stop_runtime: CommandRuntime,
) -> None:
    process = launch(
        "private-shell-session",
        term="ignore",
        ignore_hangup=True,
    )
    identity = identity_of(process)
    survivor_outcome = closure.Outcome(
        survivors=(closure.Survivor("private-shell-session", identity, "shell"),)
    )
    assert status_journal.begin(home, "stop")
    with pytest.raises(report.StopIncompleteError) as caught:
        settle(survivor_outcome, stop_runtime, operator_database, window_s=0.25)
    failure = caught.value
    assert identity.live(), "the survivor outlived its SIGKILL"
    assert failure.stage == "shell-sessions"
    # meiva:note
    # The inventory names the survivor's own command line.
    assert f"pid={identity.pid}" in str(failure)
    assert "_child.py session ignore ignore" in str(failure)
    assert len(failure.survivors) == 1
    survivor = failure.survivors[0]
    assert (survivor["pid"], survivor["birth"], survivor["starttime"]) == (
        identity.pid,
        identity.birth,
        identity.starttime,
    )
    assert survivor["role"] == "shell" and survivor["service"] == "private-shell-session"
    stop_report.report_incomplete(
        failure, [("shell-sessions", 0.25)], owns_journal=True, journal_opened=True
    )
    journal = json.loads((home / "run/lifecycle-op.json").read_text())
    assert journal["complete"] and journal["result"]["rc"] == 1
    assert journal["result"]["stage"] == "shell-sessions"
    assert journal["result"]["survivors"] == failure.survivors
    assert f"pid={identity.pid}" in capsys.readouterr().err


def test_report_keeps_known_job_diagnostic_of_a_closed_session(
    home: Path,
    launch: Launcher,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    stop_runtime: CommandRuntime,
) -> None:
    armed = home / "job-armed"
    parent = launch("private-shell-session", child_armed=armed)
    deadline = time.monotonic() + 5
    while not armed.exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    children = psutil.Process(parent.pid).children()
    assert len(children) == 1
    child = OwnedProcess.capture(children[0])
    outcome = closure.Outcome(
        closed=(closed_session("private-shell-session", ((child.pid, "python"),)),),
        survivors=(closure.Survivor("private-shell-session", child, "job"),),
    )
    settle(outcome, stop_runtime, operator_database, window_s=0.35)
    assert child.live()
    diagnostic = capsys.readouterr().err
    assert f"pid={child.pid}" in diagnostic
    assert "private-shell-session" in diagnostic
    assert "inspect the process" in diagnostic


def test_report_reuses_the_native_birth_rule() -> None:
    identity = OwnedProcess.capture(psutil.Process())
    if sys.platform == "linux":
        shifted = OwnedProcess(identity.pid, identity.birth + 3600, identity.starttime)
        assert report.identity_matches(shifted)
    else:
        shifted = OwnedProcess(identity.pid, identity.birth + 0.0001, None)
        assert not report.identity_matches(shifted)
    assert report.identity_matches(identity)


@pytest.mark.requires_host("linux")
def test_unknown_identity_is_retained_without_unrelated_live_facts() -> None:
    identity = OwnedProcess(os.getpid(), 1.0, None)
    assert report.live_identities([identity]) == [identity]
    survivor = report.capture_survivor(identity, service="private-shell-session", role="job")
    assert survivor.pid == identity.pid and survivor.birth == identity.birth
    assert survivor.cmdline is None and survivor.ppid is None and survivor.status is None


@pytest.mark.parametrize(
    ("term", "ignore_hangup", "sent", "result"),
    [
        ("default", False, signal.SIGTERM, -signal.SIGTERM),
        ("default", False, signal.SIGHUP, -signal.SIGHUP),
        ("exit", False, signal.SIGTERM, 0),
        ("ignore", False, signal.SIGTERM, None),
        ("ignore", True, signal.SIGHUP, None),
    ],
)
def test_literal_session_installs_requested_signal_behavior(
    home: Path,
    term: Literal["default", "exit", "ignore"],
    ignore_hangup: bool,
    sent: int,
    result: int | None,
) -> None:
    with launched_processes(home) as create:
        process = create("signal-behavior", term=term, ignore_hangup=ignore_hangup)
        record = SessionRecord.read(home / "run/sessions/signal-behavior.json")
        assert record is not None and record.pid == record.pgid == process.pid
        assert record.create_time == psutil.Process(process.pid).create_time()
        assert os.getsid(process.pid) == process.pid
        process.send_signal(sent)
        if result is None:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
        else:
            assert process.wait(timeout=5) == result
    assert_group_drained(process.pid)
    assert process.stdout is not None and process.stdout.closed
    assert process.stderr is not None and process.stderr.closed


def test_literal_child_is_armed_in_the_owned_group_and_cleanup_preserves_failure(
    home: Path,
) -> None:
    armed = home / "child-armed"
    original = ValueError("test body failed after child readiness")
    parent: subprocess.Popen[str] | None = None
    with pytest.raises(ValueError) as raised, launched_processes(home) as create:
        parent = create("child-session", child_armed=armed)
        deadline = time.monotonic() + 5
        while not armed.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        children = psutil.Process(parent.pid).children()
        assert len(children) == 1
        child = children[0]
        assert os.getpgid(child.pid) == os.getsid(child.pid) == parent.pid
        child.send_signal(signal.SIGTERM)
        with pytest.raises(psutil.TimeoutExpired):
            child.wait(timeout=0.2)
        raise original
    assert raised.value is original
    assert parent is not None
    assert parent.returncode == -signal.SIGKILL
    assert_group_drained(parent.pid)
    assert parent.stdout is not None and parent.stdout.closed
    assert parent.stderr is not None and parent.stderr.closed


def test_literal_launcher_rejects_executable_text_as_behavior(home: Path) -> None:
    executable_text: Any = "__import__('os')._exit(97)"
    with (
        launched_processes(home) as create,
        pytest.raises(ValueError, match="invalid fixture SIGTERM behavior"),
    ):
        create("invalid-behavior", term=executable_text)
    assert not (home / "run/sessions/invalid-behavior.json").exists()
