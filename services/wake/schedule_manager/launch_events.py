# meiva:note
# The telemetry the manager emits when a relaunch is gated or refused.
from __future__ import annotations

import logging

from base import telemetry

__all__ = ["emit_gate_held", "emit_launch_refused"]

_log = logging.getLogger("services.wake.schedule_manager.manager")


def emit_gate_held(held_for_s: float) -> None:
    _log.warning(
        "recovery gate closed for %.0fs — schedule relaunches are refused until a start "
        "publishes this root's serving receipt",
        held_for_s,
    )
    telemetry.emit(
        "telemetry",
        event_name="schedule_recovery_gated",
        level="warning",
        attributes={"held_for_s": round(held_for_s, 1)},
    )


def emit_launch_refused(schedule_id: int, reason: str) -> None:
    _log.warning("schedule %s: launch refused (%s)", schedule_id, reason)
    telemetry.emit(
        "telemetry",
        event_name="schedule_launch_refused",
        level="warning",
        attributes={"schedule_id": schedule_id, "reason": reason},
    )
