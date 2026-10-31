"""Pure placement judgment for machine-placed schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

PlacementStatus = Literal["ok", "unregistered", "paused", "stopped", "offline"]


@dataclass(frozen=True)
class MachineSnapshot:
    registered: bool
    paused_at: datetime | None = None
    pause_reason: str | None = None
    stopped_at: datetime | None = None
    maintenance_reason: str | None = None
    online: bool | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True)
class PlacementVerdict:
    status: PlacementStatus
    reason: str | None = None


def judge_placement(
    *,
    machine: str,
    snapshot: MachineSnapshot,
) -> PlacementVerdict:
    """Judge the execution readiness of a schedule placed on `machine`.

    Returns status 'ok' when the machine is registered, not paused, not stopped,
    and online. Returns 'unregistered', 'paused', 'stopped', or 'offline' with
    an explanatory reason otherwise.
    """
    if not snapshot.registered:
        return PlacementVerdict(
            status="unregistered",
            reason=f"machine {machine!r} is not registered in the cluster",
        )
    if snapshot.paused_at is not None:
        return PlacementVerdict(
            status="paused",
            reason=snapshot.pause_reason or f"machine {machine!r} is paused",
        )
    if snapshot.stopped_at is not None:
        return PlacementVerdict(
            status="stopped",
            reason=snapshot.maintenance_reason or f"machine {machine!r} is stopped",
        )
    if snapshot.online is False:
        suffix = f" ({snapshot.consecutive_failures} failed probes)" if snapshot.consecutive_failures > 0 else ""
        return PlacementVerdict(
            status="offline",
            reason=f"machine {machine!r} is offline{suffix}",
        )
    return PlacementVerdict(status="ok", reason=None)
