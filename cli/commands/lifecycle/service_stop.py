# meiva:note
# Stop a unit's recorded services and persistent shell sessions.
#
# These functions prove only local recorded process identities; they do not prove
# remote drain or stop OS-managed extras. Service stops never escalate to force.
#
# Persistent shell sessions use a bounded best-effort closure: close the known shell,
# signal known shell/foreground groups, and report known leftovers.
# A surviving shell fails the stop; a known job leftover is diagnostic. Closure
# never proves that every descendant or detached process disappeared. A stopped
# service's ledger supplies only recorded birth identities, never a global scan.
# See docs/decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md.

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.events.live.bus import EventBus
from base.sessions.pty import client, closure
from base.sessions.pty.paths import ledger_path
from base.telemetry import EventPipeline
from cli.commands.data_plane.maintenance_stop_report import (
    StopIncompleteError,
    SurvivorInventory,
    capture_survivor,
    live_identities,
)
from cli.commands.data_plane.stop_waits import (
    PROCESS_CLEANUP_WAIT_S,
    PROCESS_KILL_WAIT_S,
)
from ops import pty_close_notices
from services.agent_runner.pty_sessions import ledger

# meiva:note
# The unit stop's shell-session closure uses the shared stop bounds (`data_plane/stop_waits.py`).
_SHELL_SESSION_STOP_GRACE_S = PROCESS_CLEANUP_WAIT_S
SHELL_SESSION_KILL_WAIT_S = PROCESS_KILL_WAIT_S


# meiva:note
# Refuse maintenance while any shell session is present (`live_shell_sessions`).
def require_no_shell_sessions() -> None:
    shell_sessions = live_shell_sessions()
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
def live_shell_sessions() -> list[str]:
    try:
        return [row["name"] for row in client.request("list")["sessions"]]
    except client.ServiceDownError:
        return ledger.leftovers(ledger_path())


def close_via_service(grace_s: float, kill_s: float) -> closure.Outcome:
    """Run the one closure where the shells are: in the service (raises when it is down)."""
    return client.close_all(grace_s=grace_s, kill_s=kill_s)


# meiva:note
# Close every shell session in the service, else from its ledger; with the reason owners are told.
#
# A service that is down died uncleanly before this stop (the stop never
# leaves one behind): what its ledger still names was lost to that, not to
# this stop, and its notices say so (`pty_close_notices.CRASH_REASON`).
def _close_shell_sessions_now(grace_s: float, kill_s: float) -> tuple[closure.Outcome, str]:
    try:
        return close_via_service(grace_s, kill_s), pty_close_notices.STOP_REASON
    except client.ServiceDownError:
        return ledger.sweep(ledger_path()), pty_close_notices.CRASH_REASON


# meiva:note
# Report known shells that remain alive after bounded closure.
def _shell_sessions_incomplete(outcome: closure.Outcome, stage: str) -> StopIncompleteError:
    live = {
        s.process: s
        for s in outcome.survivors
        if s.role == "shell" and live_identities([s.process])
    }
    report = [
        capture_survivor(identity, service=found.session, role=found.role)
        for identity, found in sorted(live.items(), key=lambda item: item[0].pid)
    ]
    surviving = sorted({found.session for found in live.values()})
    inventory = SurvivorInventory(survivors=report, groups=[])
    return StopIncompleteError(
        f"shell-session closure incomplete — known shells remain alive: "
        f"{[identity.pid for identity in live]} from sessions: {surviving}\n"
        f"{inventory.render(stage=stage, killed=True)}",
        stage=stage,
        survivors=[survivor.payload() for survivor in report],
    )


# meiva:note
# Report known living jobs without turning best-effort closure into a stop gate.
def _report_shell_session_job_leftovers(outcome: closure.Outcome) -> None:
    for survivor in outcome.survivors:
        if survivor.role != "shell" and live_identities([survivor.process]):
            print(
                f"shell-session closure {survivor.session!r}: known job still running: "
                f"pid={survivor.process.pid}; inspect the process before taking action",
                file=sys.stderr,
            )


# meiva:note
# The closure evidence: no shell session is live; raise at `until`.
#
# The service drops a session from its table the moment its teardown is claimed,
# so this only waits out one still tearing down; a shell session born during the
# closure fails it.
def await_no_shell_sessions(until: float, stage: str) -> None:
    while True:
        left = live_shell_sessions()
        if not left:
            return
        if time.monotonic() >= until:
            raise StopIncompleteError(
                f"shell-session closure incomplete — shell sessions still present after the closure "
                f"ended every session it captured: {left}",
                stage=stage,
            )
        time.sleep(0.05)


def _record_close_notices(
    closed: tuple[closure.ClosedSession, ...],
    reason: str,
    *,
    direct_db: bool,
    database_factory: DatabaseFactory,
    machine_name: str,
) -> None:
    """Write one closure notice per closed busy session to the database (issue #2044).

    Each entry names the session's shell and known observed job leftovers. The write is one short connection and one transaction, made
    here while the data plane is still up and closed before this returns
    (`pty_close_notices`): the batch commits or rolls back as one, so a failed
    write leaves nothing behind to skip a later re-send. An idle session or one
    that is not an agent shell yields no notice, and no notice means no
    connection. A batch that cannot be written is loud on stderr, with the text
    its owner would have read, but never fails the closure — retrying the whole
    stop would not restore the resources it closes.
    """
    notices = pty_close_notices.notices_for(
        closed,
        reason=reason,
        local_machine=machine_name,
    )
    for unwritten, exc in pty_close_notices.write_notices(
        database_factory(), EventBus.from_settings(), notices, direct=direct_db
    ):
        # The side-channel notice must never fail a closure; stay loud so the
        # gap is visible either way.
        print(
            f"closure notice for session {unwritten.name!r} (agent {unwritten.agent_id}) "
            f"could not be written: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


# meiva:note
# Close this unit's shell sessions at `meiva stop`: HUP/TERM, a bounded grace, then SIGKILL.
#
# The grace is `_SHELL_SESSION_STOP_GRACE_S`, capped by the stop's `deadline`;
# the SIGKILL leg is bounded by `SHELL_SESSION_KILL_WAIT_S` and runs even when
# the grace spent the rest of the deadline — a stop that reached its
# shell-sessions phase closes its shell sessions
# (docs/decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md). A shell that
# survives fails the stop. Known job
# leftovers are reported without failing the stop. A
# shell session still tearing down after that gets the rest of the deadline, and
# at least `SHELL_SESSION_KILL_WAIT_S`, to leave the service's table.
#
# Busy sessions whose shell is verified gone — a job the SIGKILL cut short
# included — get a closure notice for their owner agent (issue #2044),
# written to the database over one short connection here, before the data
# plane stops (`direct_db`: this unit's own Postgres, bypassing its pooler,
# rather than the gateway's database a runner-only unit dials). That holds
# when a process outlived the SIGKILL too (the notice names it) and when
# another session keeps the stop incomplete.
def close_shell_sessions(
    deadline: float,
    *,
    direct_db: bool,
    database_factory: DatabaseFactory,
    local_machine: MachineIdentity,
) -> None:
    grace_s = max(0.0, min(deadline - time.monotonic(), _SHELL_SESSION_STOP_GRACE_S))
    outcome, reason = _close_shell_sessions_now(grace_s, SHELL_SESSION_KILL_WAIT_S)
    _record_close_notices(
        outcome.closed,
        reason,
        direct_db=direct_db,
        database_factory=database_factory,
        machine_name=local_machine.name(),
    )
    _report_shell_session_job_leftovers(outcome)
    if any(s.role == "shell" and live_identities([s.process]) for s in outcome.survivors):
        raise _shell_sessions_incomplete(outcome, "shell-sessions")
    await_no_shell_sessions(
        max(deadline, time.monotonic() + SHELL_SESSION_KILL_WAIT_S), "shell-sessions"
    )


def force_close_shell_sessions() -> None:
    # meiva:note
    # Close this unit's shell sessions at `meiva stop --mode force`: no grace, no notices.
    #
    # Force skips owner notices and drain guarantees. A surviving shell still
    # fails it; known job leftovers remain diagnostics.
    outcome, _reason = _close_shell_sessions_now(0.0, SHELL_SESSION_KILL_WAIT_S)
    _report_shell_session_job_leftovers(outcome)
    if any(s.role == "shell" and live_identities([s.process]) for s in outcome.survivors):
        raise RuntimeError(
            f"force stop did not close shell sessions: {sorted({s.session for s in outcome.survivors if s.role == 'shell'})}"
        )


def stop_data_plane(
    timeout: float,
    *,
    save: bool = True,
    notes: list[str] | None = None,
    clients: list[str] | None = None,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    producer: Callable[[], EventPipeline],
) -> list[str]:
    """Stop this home's native data plane; never stop a remote-managed plane.

    `notes` collects what the stop report must say (a Postgres shutdown that had to be
    escalated); `clients` collects the pooler's still-connected clients, reported and
    never acted on.
    """
    from cli.commands.data_plane.config import data_plane_config_from_settings
    from cli.commands.data_plane.maintenance_stop import stop

    return stop(
        data_plane_config_from_settings(),
        timeout,
        save=save,
        notes=notes,
        clients=clients,
        retained_children=retained_children,
        producer=producer,
    )
