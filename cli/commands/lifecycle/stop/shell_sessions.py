# meiva:note
# Stop a unit's persistent shell sessions.
#
# These functions prove only local recorded process identities; they do not prove
# remote drain or stop OS-managed extras. Service stops never escalate to force.
#
# Persistent shell sessions use a bounded best-effort closure: close the known shell,
# signal known shell/foreground groups, and report known leftovers.
# A surviving shell fails the stop; a known job leftover is diagnostic. Closure
# never proves that every descendant or detached process disappeared. A stopped
# service's ledger supplies only recorded birth identities, never a global scan.
# What the closure's outcome means (notices, leftovers, failure, the wait for an empty table)
# is decided by `closure_plan`; this module observes, signals through the service and acts.
# See docs/decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md.

from __future__ import annotations

import sys
from pathlib import Path
from typing import assert_never

from base.agents.context.clients import DatabaseFactory
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.events.live.bus import EventBus, event_bus_config_from_config
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from base.paths import run_dir
from base.sessions.pty import client, closure
from base.sessions.pty.paths import ledger_path, service_socket_path
from base.sessions.pty.survivor_role import SurvivorRole
from cli.commands.data_plane.maintenance.survivors import SurvivorInventory
from cli.commands.data_plane.maintenance_stop_report import (
    StopIncompleteError,
    capture_survivor,
    live_identities,
)
from cli.commands.data_plane.stop_budget import (
    closure_grace_s,
    poll_delay,
    settle_until,
    time_left,
)
from cli.commands.data_plane.stop_waits import PROCESS_CLEANUP_WAIT_S, PROCESS_KILL_WAIT_S
from cli.commands.lifecycle.stop.closure_plan import (
    AwaitNoSessions,
    FailShells,
    ReportJobLeftover,
    SurvivorFact,
    WriteNotices,
    forced_failure_message,
    job_leftover_line,
    plan_after_closure,
    shells_alive_header,
)
from cli.database.runtime import CommandRuntime
from ops import pty_close_notices
from services.agent_runner.pty_sessions import ledger

# meiva:note
# The unit stop's shell-session closure uses the shared stop bounds (`data_plane/stop_waits.py`).
SHELL_SESSION_STOP_WAIT_S = PROCESS_CLEANUP_WAIT_S
SHELL_SESSION_KILL_WAIT_S = PROCESS_KILL_WAIT_S

# meiva:note
# How often the closure evidence re-reads the service's table while a session tears down.
_SETTLE_POLL_S = 0.05


# meiva:note
# Refuse maintenance while any shell session is present (`live_shell_sessions`).
def require_no_shell_sessions(home: Path) -> None:
    shell_sessions = live_shell_sessions(home)
    if shell_sessions:
        raise RuntimeError(
            "persistent shell sessions/schedules require their own completed-work boundary; "
            f"maintenance will not kill or replay them: {shell_sessions}"
        )


# meiva:note
# Every live shell session: the service's sessions, or a dead service's leftovers.
#
# With the service listening its listing is the truth. Without it, what its ledger
# names and still runs is a crash's leftover: a process that outlived the hangup.
def live_shell_sessions(home: Path) -> list[str]:
    run = run_dir(home)
    try:
        return [
            row["name"]
            for row in client.request("list", socket_path=service_socket_path(run))["sessions"]
        ]
    except client.ServiceDownError:
        return ledger.leftovers(ledger_path(run))


# meiva:note
# Close every shell session in the service, else from its ledger; with the reason owners are told.
#
# A service that is down died uncleanly before this stop (the stop never
# leaves one behind): what its ledger still names was lost to that, not to
# this stop, and its notices say so (`pty_close_notices.CRASH_REASON`).
def run_closure(home: Path, grace_s: float, kill_s: float) -> tuple[closure.Outcome, str]:
    run = run_dir(home)
    try:
        return client.close_all(
            grace_s=grace_s, kill_s=kill_s, socket_path=service_socket_path(run)
        ), pty_close_notices.STOP_REASON
    except client.ServiceDownError:
        return ledger.sweep(
            ledger_path(run), process_control=SYSTEM_PROCESS_CONTROL
        ), pty_close_notices.CRASH_REASON


# meiva:note
# Which of the known survivors are still their recorded process, read now.
#
# A role the closure's vocabulary does not name raises `ValueError` here, before any notice is
# written or any session is judged.
def _survivor_facts(outcome: closure.Outcome) -> tuple[SurvivorFact, ...]:
    return tuple(
        SurvivorFact(
            session=survivor.session,
            role=survivor.role,
            pid=survivor.process.pid,
            alive=bool(live_identities([survivor.process])),
        )
        for survivor in outcome.survivors
    )


# meiva:note
# Report known shells that remain alive after bounded closure.
def _shell_sessions_incomplete(
    outcome: closure.Outcome, failure: FailShells, stage: str
) -> StopIncompleteError:
    live = {
        s.process: s
        for s in outcome.survivors
        if s.role is SurvivorRole.SHELL and live_identities([s.process])
    }
    report = [
        capture_survivor(identity, service=found.session, role=found.role.value)
        for identity, found in sorted(live.items(), key=lambda item: item[0].pid)
    ]
    inventory = SurvivorInventory(survivors=tuple(report))
    return StopIncompleteError(
        f"{shells_alive_header(failure)}\n{inventory.render(stage=stage, killed=True)}",
        stage=stage,
        survivors=[survivor.payload() for survivor in report],
    )


# meiva:note
# The closure evidence: no shell session is live; raise at `until`.
#
# The service drops a session from its table the moment its teardown is claimed, so this only
# waits out one still tearing down; a shell session born during the closure fails it.
def await_no_shell_sessions(home: Path, until: float, stage: str) -> None:
    while True:
        left = live_shell_sessions(home)
        if not left:
            return
        delay = poll_delay(time_left(until, SYSTEM_TIMERS.monotonic()), _SETTLE_POLL_S)
        if delay is None:
            raise StopIncompleteError(
                f"shell-session closure incomplete — shell sessions still present after the "
                f"closure ended every session it captured: {left}",
                stage=stage,
            )
        SYSTEM_TIMERS.sleep(delay)


# meiva:note
# Write one closure notice per closed busy session to the database (issue #2044).
#
# Each entry names the session's shell and known observed job leftovers. The write is one short
# connection and one transaction, made here while the data plane is still up and closed before
# this returns (`pty_close_notices`): the batch commits or rolls back as one, so a failed write
# leaves nothing behind to skip a later re-send. An idle session or one that is not an agent
# shell yields no notice, and no notice means no connection. A batch that cannot be written is
# loud on stderr, with the text its owner would have read, but never fails the closure:
# retrying the whole stop would not restore the resources it closes.
def _write_close_notices(
    closed: tuple[closure.ClosedSession, ...],
    reason: str,
    *,
    direct_db: bool,
    database_factory: DatabaseFactory,
    command: CommandRuntime,
) -> None:
    notices = pty_close_notices.notices_for(
        closed,
        reason=reason,
        local_machine=command.machine.name(),
        closed_at=Clock(
            ClockConfig("UTC", None, message_timestamp_weekday=False), timers=command.timers
        )
        .now()
        .isoformat(),
    )
    if not notices:
        return
    bus = EventBus(event_bus_config_from_config(command.config))
    for unwritten, exc in pty_close_notices.write_notices(
        database_factory(), bus, notices, direct=direct_db
    ):
        # meiva:note
        # The side-channel notice must never fail a closure; stay loud so the
        # gap is visible either way.
        print(
            f"closure notice for session {unwritten.name!r} (agent {unwritten.agent_id}) "
            f"could not be written: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


# meiva:note
# Act on a closure's outcome: notify the owners, report leftovers, then fail or settle.
#
# The notices are written before anything can fail, so a closed session's owner is told even
# when another session leaves the stop incomplete (a retry could never record it: the service
# no longer lists it). Busy sessions whose shell is verified gone, a job the SIGKILL cut short
# included, get their notice; the database is this unit's own Postgres bypassing its pooler
# (`direct_db`) or the gateway's database a runner-only unit dials.
def settle_closure(
    outcome: closure.Outcome,
    reason: str,
    *,
    deadline: float,
    direct_db: bool,
    database_factory: DatabaseFactory,
    command: CommandRuntime,
) -> None:
    for step in plan_after_closure(_survivor_facts(outcome), forced=False):
        match step:
            case WriteNotices():
                _write_close_notices(
                    outcome.closed,
                    reason,
                    direct_db=direct_db,
                    database_factory=database_factory,
                    command=command,
                )
            case ReportJobLeftover():
                print(job_leftover_line(step), file=sys.stderr)
            case FailShells():
                raise _shell_sessions_incomplete(outcome, step, "shell-sessions")
            case AwaitNoSessions():
                until = settle_until(deadline, SYSTEM_TIMERS.monotonic(), SHELL_SESSION_KILL_WAIT_S)
                await_no_shell_sessions(command.home, until, "shell-sessions")
            case _:
                assert_never(step)


# meiva:note
# Close this unit's shell sessions at `meiva stop`: HUP/TERM, a bounded grace, then SIGKILL.
#
# The grace is `cleanup_wait_s`, capped by the stop's `deadline`; the SIGKILL leg is bounded by
# `SHELL_SESSION_KILL_WAIT_S` and runs even when the grace spent the rest of the deadline: a stop
# that reached its shell-sessions phase closes its shell sessions
# (docs/decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md). A shell that
# survives fails the stop; known job leftovers are reported without failing it. A shell session
# still tearing down after that gets the rest of the deadline, and at least
# `SHELL_SESSION_KILL_WAIT_S`, to leave the service's table.
def close_shell_sessions(
    deadline: float,
    *,
    direct_db: bool,
    database_factory: DatabaseFactory,
    command: CommandRuntime,
    cleanup_wait_s: float,
) -> None:
    grace_s = closure_grace_s(deadline - SYSTEM_TIMERS.monotonic(), cleanup_wait_s)
    outcome, reason = run_closure(command.home, grace_s, SHELL_SESSION_KILL_WAIT_S)
    settle_closure(
        outcome,
        reason,
        deadline=deadline,
        direct_db=direct_db,
        database_factory=database_factory,
        command=command,
    )


# meiva:note
# Act on a forced closure's outcome: report the leftover jobs, fail on a surviving shell.
def settle_forced_closure(outcome: closure.Outcome) -> None:
    for step in plan_after_closure(_survivor_facts(outcome), forced=True):
        match step:
            case ReportJobLeftover():
                print(job_leftover_line(step), file=sys.stderr)
            case FailShells():
                raise RuntimeError(forced_failure_message(step))
            case WriteNotices() | AwaitNoSessions():
                raise AssertionError("a forced closure plans no notices and no wait")
            case _:
                assert_never(step)


# meiva:note
# Close this unit's shell sessions at `meiva stop --mode force`: no grace, no notices.
#
# Force skips owner notices and drain guarantees. A surviving shell still fails it; known job
# leftovers remain diagnostics.
def force_close_shell_sessions(home: Path) -> None:
    outcome, _reason = run_closure(home, 0.0, SHELL_SESSION_KILL_WAIT_S)
    settle_forced_closure(outcome)
