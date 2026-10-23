# meiva:note
# When the heartbeat guard checks, and what one reading of the heartbeat means.
#
# `gateway.lgtm.telemetry_staleness` owns the effects (the heartbeat query, the lock, the event
# emission); the transition rules are values in, decision out:
#
# - `check_due`: whether enough time has passed since the last check to ask again;
# - `staleness_step`: given one source's state and the heartbeat's age, whether the read is stale,
#   the state to keep, and the event to emit, if any.
#
# Five minutes is five times the heartbeat cadence. It is deliberately not three times the
# 15-second metric export interval: a 45-second deadline for a 60-second heartbeat would alert
# during healthy operation.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "HEARTBEAT_EVENT",
    "STALENESS_THRESHOLD_S",
    "SourceState",
    "StalenessEvent",
    "StalenessStep",
    "check_due",
    "staleness_step",
]

HEARTBEAT_EVENT = "gateway_latency"
STALENESS_THRESHOLD_S = 300


# meiva:note
# A source that is currently stale: since when, and when it was last reported.
@dataclass(frozen=True)
class SourceState:
    stale_since: float
    last_reported: float


# meiva:note
# A transition event to emit: its name and attributes.
@dataclass(frozen=True)
class StalenessEvent:
    name: str
    attributes: dict[str, object]


# meiva:note
# The verdict of one reading: whether it reads stale, the state to keep, the event to emit.
@dataclass(frozen=True)
class StalenessStep:
    stale: bool
    state: SourceState | None
    event: StalenessEvent | None


# meiva:note
# Whether a new heartbeat check is due: never checked, the interval has passed, or the
# clock stepped back past the last check.
def check_due(last_checked_s: float | None, now_s: float, interval_s: float) -> bool:
    return last_checked_s is None or now_s < last_checked_s or now_s - last_checked_s >= interval_s


# meiva:note
# One reading of `source`'s heartbeat age (None = no heartbeat row) against its state.
#
# A stale source emits `entered` once, `ongoing` once per threshold while it stays stale, and
# `telemetry_read_recovered` when it reads fresh again.
def staleness_step(
    source: str, state: SourceState | None, age_s: float | None, now_s: float
) -> StalenessStep:
    if age_s is None or age_s > STALENESS_THRESHOLD_S:
        reason = "heartbeat missing" if age_s is None else "heartbeat older than threshold"
        if state is None:
            kept, action = SourceState(stale_since=now_s, last_reported=now_s), "entered"
        elif now_s - state.last_reported >= STALENESS_THRESHOLD_S:
            kept, action = SourceState(state.stale_since, now_s), "ongoing"
        else:
            return StalenessStep(stale=True, state=state, event=None)
        return StalenessStep(
            stale=True,
            state=kept,
            event=StalenessEvent(
                "telemetry_read_stale",
                {
                    "source": source,
                    "signal": HEARTBEAT_EVENT,
                    "threshold_s": STALENESS_THRESHOLD_S,
                    "age_s": age_s,
                    "action": action,
                    "reason": reason,
                },
            ),
        )
    if state is None:
        return StalenessStep(stale=False, state=None, event=None)
    return StalenessStep(
        stale=False,
        state=None,
        event=StalenessEvent(
            "telemetry_read_recovered",
            {
                "source": source,
                "signal": HEARTBEAT_EVENT,
                "stale_duration_s": now_s - state.stale_since,
            },
        ),
    )
