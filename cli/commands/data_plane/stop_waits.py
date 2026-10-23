# meiva:note
# Bounded stop waits and the Postgres escalation report, shared by the data-plane stop
# (`maintenance_stop`) and the unit stop (`cli/commands/lifecycle/stop/shell_sessions.py`).
#
# They live under `data_plane` because the data-plane stop is the lower caller: the unit stop
# imports them, never the reverse.

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from base import telemetry
from base.cluster import postgres as owned_postgres
from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from base.telemetry import EventPipeline
from cli.commands.data_plane.stop_budget import poll_delay, require_valid_timeout, time_left

# meiva:note
# How long a normal stop's shell-session closure waits between its HUP/TERM and the
# SIGKILL of whatever is left (docs/decisions/runtime/processes/shutdown/2026-09-28-stop-escalates-to-sigkill.md),
# and how long Postgres' immediate shutdown is given to finish at the data-plane stop: a job
# that handles TERM gets this long to clean up. The stop's own deadline caps it as well.
PROCESS_CLEANUP_WAIT_S = 10.0

# meiva:note
# The SIGKILL leg's own bound at a normal stop: each wait inside a session kill,
# the wait for the killed sessions' hosts to end, and the least the closure
# evidence waits (it also gets the rest of the stop deadline); at the data-plane stop, how
# long the SIGKILL of what outlives the immediate shutdown is waited for.
# Every wait ends as soon as its processes are gone. The leg runs even when the
# grace spent the rest of the stop deadline — a stop that reached its shell-sessions
# phase closes its shell sessions — so a stop can overrun its deadline by this
# bounded leg.
PROCESS_KILL_WAIT_S = 3.0

# meiva:note
# How often a wait for processes to exit observes them again.
_POLL_INTERVAL_S = 0.05


def deadline_after(timeout: float) -> float:
    return time.monotonic() + require_valid_timeout(timeout)


def remaining(deadline: float) -> float:
    left = time_left(deadline, time.monotonic())
    if left is None:
        raise TimeoutError("maintenance kept its hold; stop deadline expired")
    return left


# meiva:note
# Wait for the `tracked` identities to empty, within `deadline`.
#
# Never escalates a signal and never certifies success while anything is alive. A
# descendant of a living identity joins `tracked`, so a leader that exits without closing
# its children does not let the wait finish early.
def wait_for_exit(tracked: set[OwnedProcess], deadline: float) -> None:
    while True:
        living = {identity for identity in tracked if identity.live()}
        if not living:
            return
        for identity in living:
            retain_processes(tracked, capture_tree(identity))
        delay = poll_delay(time_left(deadline, time.monotonic()), _POLL_INTERVAL_S)
        if delay is None:
            raise TimeoutError(
                "maintenance kept its hold; processes did not exit: "
                f"{sorted(identity.pid for identity in living)}"
            )
        time.sleep(delay)


# meiva:note
# Report a Postgres shutdown that had to be ended by an immediate one.
#
# The owner (`base.cluster.postgres`) logs the escalation; this adds the
# operator line and the `postgres_stop_escalated` event. `notes`, when the
# caller owns a stop journal, collects the line for it
# (`stop.report.finish_stop`); a leg that owns no journal passes
# nothing and still gets stderr and the event
# (docs/decisions/data/database/2026-10-02-pg-stop-escalates-to-immediate.md).
def report_postgres_stop_escalation(
    escalation: owned_postgres.Escalation,
    notes: list[str] | None = None,
    *,
    producer: Callable[[], EventPipeline],
) -> None:
    killed = ", ".join(str(pid) for pid in escalation.killed) or "none"
    note = (
        f"postgres {escalation.detail}; ended by an immediate shutdown "
        f"(crash recovery at the next start; unarchived WAL stays in pg_wal), "
        f"killed leftover processes: {killed}"
    )
    print(f"  ! {note}", file=sys.stderr, flush=True)
    telemetry.emit(
        "telemetry",
        "postgres_stop_escalated",
        producer=producer,
        level="error",
        source="stop",
        attributes={"detail": escalation.detail, "killed": list(escalation.killed)},
    )
    if notes is not None:
        notes.append(note)
