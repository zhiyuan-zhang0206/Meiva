# meiva:note
# What the roster does for each machine: render the local row, render a snapshot, or dial.
#
# `plan_roster` is the whole decision of which machines are probed: the gateway's own machine
# without the agent-runner capability reads itself, a machine with a fresh heartbeat snapshot
# renders from it and nothing is dialed, and every other machine (no snapshot, a stale one, or an
# explicit fresh read that passes no snapshots) is dialed. `snapshot_verdict` grades a snapshot.
# `gateway.cluster.status` executes the plan.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from gateway.cluster.roster.liveness_snapshot import Snapshot
from gateway.cluster.roster.roster_rows import RosterRowSpec

__all__ = ["RosterStep", "SnapshotVerdict", "StepKind", "plan_roster", "snapshot_verdict"]


class StepKind(StrEnum):
    LOCAL = "local"
    SNAPSHOT = "snapshot"
    DIAL = "dial"


# meiva:note
# How a heartbeat snapshot reads as a roster row.
class SnapshotVerdict(StrEnum):
    OFFLINE = "offline"
    UNKNOWN = "unknown"
    REPORTED = "reported"


# meiva:note
# One machine and what the roster does for it.
@dataclass(frozen=True)
class RosterStep:
    kind: StepKind
    row: RosterRowSpec
    snapshot: Snapshot | None


# meiva:note
# The step for every machine, in the order given.
#
# `snapshots=None` is an explicit fresh read: nothing is rendered from the heartbeat pass.
def plan_roster(
    rows: Sequence[RosterRowSpec],
    local_name: str,
    snapshots: Mapping[str, Snapshot] | None,
    *,
    now: datetime,
) -> tuple[RosterStep, ...]:
    steps: list[RosterStep] = []
    for row in rows:
        if row.name == local_name and "agent-runner" not in row.role:
            steps.append(RosterStep(StepKind.LOCAL, row, None))
            continue
        snapshot = None if snapshots is None else snapshots.get(row.name)
        if snapshot is not None and snapshot.fresh(now):
            steps.append(RosterStep(StepKind.SNAPSHOT, row, snapshot))
        else:
            steps.append(RosterStep(StepKind.DIAL, row, None))
    return tuple(steps)


# meiva:note
# Two consecutive failed passes (or a failed pass with no earlier answer to show) read
# offline; one dropped probe keeps the last status on screen, and its age says so. A
# reachable answer that is not a ClusterStatus is the documented online + paused=None
# abnormal state.
def snapshot_verdict(snapshot: Snapshot) -> SnapshotVerdict:
    if snapshot.failed_enough():
        return SnapshotVerdict.OFFLINE
    if snapshot.status is None:
        return SnapshotVerdict.UNKNOWN
    return SnapshotVerdict.REPORTED
