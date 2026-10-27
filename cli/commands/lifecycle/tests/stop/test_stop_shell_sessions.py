# meiva:note
# Shell-session closure at a normal stop (#2045): the stop's side of the pty-sessions service.
#
# The service holds every shell and runs the one shell-session closure
# (`base.sessions.pty.closure`, covered by `services/agent_runner/pty_sessions/tests/test_close_all.py`).
# What the closure's outcome means is `closure_plan`'s (`test_closure_plan.py`). These tests
# lock what `meiva stop` does around the closure, with real shells where the service is the
# thing under test and the closure's outcome handed in as a value where it cannot be made:
#
# - a stop asks the service to close everything within a grace capped by the stop's deadline,
#   writes one owner notice per closed busy session, and fails when a shell survives; known job
#   leftovers are diagnostic;
# - a stop finding the service gone closes the leftovers its ledger names;
# - `live_shell_sessions` reads the service while it listens and the ledger when it does not;
# - `--force` closes shell sessions with no grace and no notices.
#
# An outcome with a survivor stands in for processes the closure may not signal (another
# user's, such as a root `sudo` on the pty), which no test can create for real.

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import psycopg
import pytest

from base.native_process.ownership import OwnedProcess
from base.sessions.pty import client, closure
from base.sessions.pty.paths import ledger_path
from base.sessions.pty.survivor_role import SurvivorRole
from base.sessions.pty.tests import pty_jobs as jobs
from base.sessions.pty.tests import pty_shells as support
from base.sessions.pty.tests.pty_reaper import PtyReaper
from base.sessions.pty.tests.pty_service import service_run, service_socket
from base.sessions.pty.tests.pty_shells import new, wait_for
from base.telemetry import EventPipeline
from cli.commands.data_plane.maintenance_stop_report import StopIncompleteError
from cli.commands.lifecycle.stop import do_stop
from cli.commands.lifecycle.stop import shell_sessions as shells
from cli.commands.lifecycle.tests.stop.stop_support import (
    Launcher,
    PtyServiceProcess,
    RefusingPtyService,
    busy_session,
    closed_session,
    identity_of,
    stop_environment,
    stop_request,
    written_notices,
)
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import launch as launch
from cli.commands.lifecycle.tests.stop.stop_support import pty_service as pty_service
from cli.commands.lifecycle.tests.stop.stop_support import (
    refusing_pty_service as refusing_pty_service,
)
from cli.commands.lifecycle.tests.stop.stop_support import stop_runtime as stop_runtime
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime
from ops import pty_close_notices
from services.agent_runner.pty_sessions import ledger

CHILD = Path(__file__).with_name("_child.py")

# meiva:note
# The idle test's own prompt, set by its home's `.bash_profile`.
_IDLE_PROMPT = "meiva-idle-shell>"


def names(conn: psycopg.Connection) -> list[str]:
    return [notice["name"] for notice in written_notices(conn)]


# meiva:note
# The stop's handling of a closure's outcome, as the shell-sessions phase runs it.
def settle(
    outcome: closure.Outcome,
    runtime: CommandRuntime,
    database_factory: OperatorDatabaseFactory,
    *,
    window_s: float = 12.0,
) -> None:
    shells.settle_closure(
        outcome,
        pty_close_notices.STOP_REASON,
        deadline=time.monotonic() + window_s,
        direct_db=False,
        database_factory=database_factory,
        command=runtime,
    )


# meiva:note
# A regular interruptible foreground job closes via a normal stop, through the
# service, and its owner gets the closure notice.
@pytest.mark.flaky
def test_stop_closes_busy_shell_session_job_with_real_signals(
    redis_url: str,
    home: Path,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    name = "meiva-agent-987-shell-2045-job"
    _, running = busy_session(home, name, jobs.TERM_OK, pty_reaper)
    assert running, "the synthetic job never started"

    rc = do_stop(
        stop_request(timeout=15),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    assert not client.has_session(name, socket_path=service_socket())
    assert jobs.wait_exit(running[0].pid), "the job must be closed by the normal stop"
    assert names(db_conn) == [name]


# meiva:note
# A job that ignores TERM and HUP is SIGKILLed once the stop's grace ends: the
# stop completes well inside its deadline, and the owner still gets the closure
# notice for the work the kill cut short.
@pytest.mark.flaky
def test_stop_kills_a_job_that_ignores_termination_after_its_grace(
    redis_url: str,
    home: Path,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    name = "meiva-agent-987-shell-2045-stubborn"
    _, running = busy_session(home, name, jobs.STUBBORN, pty_reaper, ready_line="stubborn-ready")
    assert running, "the stubborn job never started"

    started = time.monotonic()
    rc = do_stop(
        stop_request(timeout=12),
        stop_environment(
            stop_runtime, operator_database, operator_pipeline, shell_cleanup_wait_s=1.0
        ),
        retained_children=[],
    )
    assert rc == 0
    assert time.monotonic() - started < 8, "the grace bounds the wait, not the stop deadline"
    assert jobs.wait_exit(running[0].pid, timeout=5), "the job outlived the stop"
    assert not client.has_session(name, socket_path=service_socket())
    assert len(written_notices(db_conn)) == 1, "a killed busy session still leaves its notice"


# meiva:note
# An idle shell (no jobs) closed by stop is silent: the TTL reaper's quiet-empty
# policy, never a blanket close notification (issue #2044 #3).
#
# The session has no initial command, so idle is the login shell at its first
# prompt. The test owns that prompt: the login shell reads this home's
# `.bash_profile` after the system's files, so the prompt is `_IDLE_PROMPT`
# whatever the host's or the developer's shell configuration prints.
def test_stop_records_nothing_for_idle_shell(
    redis_url: str,
    home: Path,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    (home / ".bash_profile").write_text(f"PS1='{_IDLE_PROMPT} '\n")
    name = "meiva-agent-987-shell-2044-idle"
    assert new(name, home, {"MEIVA_HOME": str(home), "HOME": str(home)})
    pty_reaper.track_session(name)
    assert wait_for(lambda: support.screen(name).rstrip().endswith(_IDLE_PROMPT)), (
        "the login shell never printed its prompt"
    )

    rc = do_stop(
        stop_request(timeout=15),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    assert not client.has_session(name, socket_path=service_socket())
    assert written_notices(db_conn) == []


# meiva:note
# A session whose shell exited on its own before the stop is already gone from
# the service: it must not fail the stop.
@pytest.mark.flaky
def test_stop_tolerates_naturally_exited_session(
    home: Path,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    name = "meiva-agent-987-shell-2045-already-dead"
    shell, _ = busy_session(home, name, jobs.TERM_OK, pty_reaper)
    os.kill(shell.pid, signal.SIGKILL)
    assert wait_for(lambda: not client.has_session(name, socket_path=service_socket())), (
        "the service kept the dead session"
    )
    rc = do_stop(
        stop_request(timeout=10),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0


# meiva:note
# One session closes; in another the shell itself outlives the stop. The stop is
# incomplete, yet the closed session's notice is recorded before it reports: the
# service no longer lists it, so a retry could never record it. The session whose
# shell still lives records nothing and is left to the retry, which records it:
# each notice once.
@pytest.mark.flaky
def test_incomplete_stop_still_records_the_sessions_it_closed(
    redis_url: str,
    home: Path,
    launch: Launcher,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    stop_runtime: CommandRuntime,
) -> None:
    closed = "meiva-agent-987-shell-2051-closed"
    stuck = "meiva-agent-987-shell-2052-stuck"
    shell = identity_of(launch(stuck, term="ignore", ignore_hangup=True))
    first = closure.Outcome(
        closed=(closed_session(closed),),
        survivors=(closure.Survivor(stuck, shell, SurvivorRole.SHELL),),
    )

    with pytest.raises(StopIncompleteError) as incomplete:
        settle(first, stop_runtime, operator_database)
    assert stuck in str(incomplete.value) and f"pid={shell.pid}" in str(incomplete.value)
    assert incomplete.value.stage == "shell-sessions"
    recorded = written_notices(db_conn)
    assert [notice["name"] for notice in recorded] == [closed], (
        "the closed session's notice was lost"
    )
    assert "survivors" not in recorded[0], "nothing of the closed session survived"

    settle(closure.Outcome(closed=(closed_session(stuck),)), stop_runtime, operator_database)
    assert names(db_conn) == [closed, stuck]
    settle(closure.Outcome(closed=(closed_session(stuck),)), stop_runtime, operator_database)
    assert names(db_conn) == [closed, stuck], "a notice written twice is delivered once"


# meiva:note
# A known job can survive closure: report it and notify its owner, but release
# stop successfully once the shell session is closed.
def test_known_job_leftover_is_reported_without_failing_stop(
    redis_url: str,
    home: Path,
    launch: Launcher,
    capsys: pytest.CaptureFixture[str],
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    stop_runtime: CommandRuntime,
) -> None:
    name = "meiva-agent-987-shell-2044-stubborn"
    job = launch("private-job", term="ignore", ignore_hangup=True)
    outcome = closure.Outcome(
        closed=(closed_session(name, ((job.pid, "python3"),)),),
        survivors=(closure.Survivor(name, identity_of(job), SurvivorRole.JOB),),
    )

    settle(outcome, stop_runtime, operator_database)

    assert psutil.pid_exists(job.pid)
    err = capsys.readouterr().err
    assert name in err and f"pid={job.pid}" in err and "inspect the process" in err
    recorded = written_notices(db_conn)
    assert [notice["name"] for notice in recorded] == [name]
    assert recorded[0]["survivors"] == [{"pid": job.pid, "name": "python3"}]

    settle(outcome, stop_runtime, operator_database)
    assert len(written_notices(db_conn)) == 1, "the retry recorded the closed session again"


# meiva:note
# The report names only survivors that are still their captured process: one that
# exited after the closure's verdict is gone, and the stop completes.
def test_a_survivor_that_died_before_the_report_does_not_fail_the_stop(
    launch: Launcher,
    operator_database: OperatorDatabaseFactory,
    stop_runtime: CommandRuntime,
) -> None:
    proc = launch("private-gone", term="ignore", ignore_hangup=True)
    gone = identity_of(proc)
    proc.kill()
    proc.wait(timeout=5)
    settle(
        closure.Outcome(survivors=(closure.Survivor("gone", gone, SurvivorRole.SHELL),)),
        stop_runtime,
        operator_database,
    )


# meiva:note
# A shell session still present once the closure ended every session it captured
# fails the stop, naming it, and without maintenance's claim that nothing will be
# killed: this stop has just killed its sessions.
def test_a_shell_session_left_after_the_closure_fails_the_stop_in_its_own_words(
    home: Path, launch: Launcher
) -> None:
    name = "meiva-agent-987-shell-2053-lingering"
    proc = launch(name, term="ignore", ignore_hangup=True)
    ledger.write(ledger_path(service_run()), [closure.Target(name, identity_of(proc))])

    with pytest.raises(StopIncompleteError) as excinfo:
        shells.await_no_shell_sessions(home, time.monotonic(), "shell-sessions")
    message = str(excinfo.value)
    assert name in message
    assert "will not kill" not in message
    assert excinfo.value.stage == "shell-sessions"


# meiva:note
# The closure evidence waits until the stop's deadline, and at least the SIGKILL
# leg's bound: a session still tearing down past that bound but gone from the
# service before the deadline is a closed shell session, not a failed stop.
def test_a_shell_session_that_clears_within_the_stop_deadline_does_not_fail_the_stop(
    home: Path,
) -> None:
    name = "meiva-agent-987-shell-2053-tearing-down"
    tearing_down = subprocess.Popen([sys.executable, str(CHILD), "sleep", "0.6"])
    ledger.write(ledger_path(service_run()), [closure.Target(name, identity_of(tearing_down))])
    assert shells.live_shell_sessions(home) == [name]
    started = time.monotonic()
    try:
        shells.await_no_shell_sessions(home, started + 10, "shell-sessions")
    finally:
        tearing_down.wait(timeout=10)
    assert 0.4 <= time.monotonic() - started < 5, "it waited for the session to leave the table"
    assert shells.live_shell_sessions(home) == []


# meiva:note
# With the service listening its listing is the truth, and any session in it
# refuses maintenance.
def test_live_shell_sessions_reads_the_service_while_it_listens(
    home: Path, pty_service: PtyServiceProcess
) -> None:
    assert shells.live_shell_sessions(home) == []
    shells.require_no_shell_sessions(home)
    name = "meiva-agent-987-shell-2053-listed"
    assert new(name, home, {"HOME": str(home)})

    assert shells.live_shell_sessions(home) == [name]
    with pytest.raises(RuntimeError, match=name):
        shells.require_no_shell_sessions(home)


# meiva:note
# A service that is not listening has no sessions; a crash's leftover is a process
# its ledger names that is still its recorded process.
def test_live_shell_sessions_without_the_service_is_what_its_ledger_still_runs(
    home: Path, launch: Launcher
) -> None:
    assert shells.live_shell_sessions(home) == []
    name = "meiva-agent-987-shell-2053-leftover"
    proc = launch(name, term="ignore", ignore_hangup=True)
    ledger.write(ledger_path(service_run()), [closure.Target(name, identity_of(proc))])
    assert shells.live_shell_sessions(home) == [name]
    proc.kill()
    proc.wait(timeout=5)
    assert shells.live_shell_sessions(home) == []


# meiva:note
# No service listens, so there is no one to ask: the stop closes from the ledger
# the dead service left, the leftover hung up first and SIGKILLed when it ignores
# the hangup, and the ledger is then empty. Its owners are told the service ended
# uncleanly, not that the stop closed their session.
@pytest.mark.parametrize("ignores_hangup", [False, True])
def test_a_stop_without_the_service_closes_the_leftovers_its_ledger_names(
    ignores_hangup: bool,
    home: Path,
    launch: Launcher,
    operator_database: OperatorDatabaseFactory,
    stop_runtime: CommandRuntime,
) -> None:
    name = "meiva-agent-987-shell-2054-orphaned"
    proc = launch(
        name, term="ignore" if ignores_hangup else "default", ignore_hangup=ignores_hangup
    )
    ledger.write(ledger_path(service_run()), [closure.Target(name, identity_of(proc))])
    assert shells.live_shell_sessions(home) == [name]

    shells.close_shell_sessions(
        time.monotonic() + 10,
        direct_db=False,
        database_factory=operator_database,
        command=stop_runtime,
        cleanup_wait_s=shells.SHELL_SESSION_STOP_WAIT_S,
    )
    assert proc.wait(timeout=10) == (-signal.SIGKILL if ignores_hangup else -signal.SIGHUP)
    assert shells.live_shell_sessions(home) == []
    assert ledger.read(ledger_path(service_run())) == []


def test_a_stop_without_the_service_tells_owners_the_service_crashed_not_that_it_stopped(
    redis_url: str,
    home: Path,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    stop_runtime: CommandRuntime,
) -> None:
    child = subprocess.Popen(["true"])
    gone = identity_of(child)
    child.wait(timeout=10)
    name = "meiva-agent-987-shell-2044-crashed"
    ledger.write(
        ledger_path(service_run()),
        [closure.Target(name, gone, (gone, OwnedProcess(gone.pid, 0.0, None)))],
    )

    shells.close_shell_sessions(
        time.monotonic() + 15,
        direct_db=False,
        database_factory=operator_database,
        command=stop_runtime,
        cleanup_wait_s=shells.SHELL_SESSION_STOP_WAIT_S,
    )

    assert [(notice["name"], notice["reason"]) for notice in written_notices(db_conn)] == [
        (name, pty_close_notices.CRASH_REASON)
    ]
    assert ledger.read(ledger_path(service_run())) == []


# meiva:note
# `--force` closes shell sessions with no grace: a job that ignores TERM and HUP is
# SIGKILLed at once, and no owner notice is written.
@pytest.mark.flaky
def test_force_close_closes_every_session_at_once_without_notices(
    redis_url: str,
    home: Path,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
    stop_runtime: CommandRuntime,
) -> None:
    name = "meiva-agent-987-shell-2055-force"
    _, running = busy_session(home, name, jobs.STUBBORN, pty_reaper, ready_line="stubborn-ready")
    assert running, "the stubborn job never started"

    started = time.monotonic()
    shells.force_close_shell_sessions(stop_runtime)
    assert time.monotonic() - started < 8
    assert not client.has_session(name, socket_path=service_socket())
    assert jobs.wait_exit(running[0].pid, timeout=5), "the job outlived the forced close"
    assert written_notices(db_conn) == []


def test_force_close_reports_known_job_without_failing(
    launch: Launcher, capsys: pytest.CaptureFixture[str]
) -> None:
    name = "meiva-agent-987-shell-2055-denied"
    job = launch("private-job", term="ignore", ignore_hangup=True)
    shells.settle_forced_closure(
        closure.Outcome(survivors=(closure.Survivor(name, identity_of(job), SurvivorRole.JOB),))
    )
    assert name in capsys.readouterr().err


# meiva:note
# A survivor whose role the closure's vocabulary does not name is not a job to report and move on
# from: the outcome is refused where the wire outcome is read, before any survivor is judged.
def test_a_closure_outcome_with_an_unknown_survivor_role_is_refused(launch: Launcher) -> None:
    name = "meiva-agent-987-shell-2055-odd"
    survivor = launch("private-odd", term="ignore", ignore_hangup=True)
    wire = closure.Outcome(
        survivors=(closure.Survivor(name, identity_of(survivor), SurvivorRole.JOB),)
    ).to_wire()
    wire["survivors"][0]["role"] = "helper"
    with pytest.raises(ValueError, match="'helper' is not a valid SurvivorRole"):
        closure.Outcome.from_wire(wire)


def test_force_close_still_fails_when_known_shell_survives(launch: Launcher) -> None:
    name = "private-surviving-shell"
    shell = launch(name, term="ignore", ignore_hangup=True)
    with pytest.raises(RuntimeError, match=name):
        shells.settle_forced_closure(
            closure.Outcome(
                survivors=(closure.Survivor(name, identity_of(shell), SurvivorRole.SHELL),)
            )
        )


@pytest.mark.parametrize("force", [False, True])
def test_operational_shell_session_close_failure_is_not_best_effort_success(
    force: bool,
    refusing_pty_service: RefusingPtyService,
    operator_database: OperatorDatabaseFactory,
    stop_runtime: CommandRuntime,
) -> None:
    with pytest.raises(client.ServiceError, match="known group signal denied"):
        if force:
            shells.force_close_shell_sessions(stop_runtime)
        else:
            shells.close_shell_sessions(
                time.monotonic() + 1,
                direct_db=False,
                database_factory=operator_database,
                command=stop_runtime,
                cleanup_wait_s=shells.SHELL_SESSION_STOP_WAIT_S,
            )
