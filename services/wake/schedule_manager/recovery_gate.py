# meiva:note
# The recovery gate: whether relaunches are held back, and when to say so.
#
# Relaunches are refused whenever the published serving receipt does not name the live root (a
# helper-reseeded root publishes none). The manager reads the receipt each tick and passes the
# fact in; these functions turn the fact, the clock reading and the previous state into the next
# state and the alerts to emit, so a sustained refusal is reported once per episode and a
# refusal once per schedule per episode.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GateState", "GateStep", "observe_gate", "refuse"]


# meiva:note
# The open episode of refusals: since when, whether it was reported, which were.
@dataclass(frozen=True)
class GateState:
    held_since: float | None = None
    alerted: bool = False
    refused: frozenset[int] = frozenset()


# meiva:note
# The state after an observation and, when the episode just crossed the alert age, how long
# it has lasted.
@dataclass(frozen=True)
class GateStep:
    state: GateState
    held_for_s: float | None


# meiva:note
# Fold one tick's reading of the receipt into the gate.
def observe_gate(
    state: GateState, *, permitted: bool, now: float, alert_after_s: float
) -> GateStep:
    if permitted:
        return GateStep(GateState(), None)
    if state.held_since is None:
        return GateStep(GateState(held_since=now), None)
    if not state.alerted and now - state.held_since >= alert_after_s:
        held_for_s = now - state.held_since
        return GateStep(
            GateState(held_since=state.held_since, alerted=True, refused=state.refused), held_for_s
        )
    return GateStep(state, None)


# meiva:note
# Record a refusal; the second element says whether it is the first of its episode.
def refuse(state: GateState, schedule_id: int) -> tuple[GateState, bool]:
    if schedule_id in state.refused:
        return state, False
    return (
        GateState(
            held_since=state.held_since,
            alerted=state.alerted,
            refused=state.refused | {schedule_id},
        ),
        True,
    )
