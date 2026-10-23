# meiva:note
# The decision half of one poll beat: forward the accumulated text, keep a silent exec visibly
# alive, and stop on cancel or deadline. `_subprocess._poll_child` reads the beat's values and
# carries out the plan. Cancel outranks the deadline on the same beat.
# meiva:link agent/graph/exec/docs/exec.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

KEEPALIVE_INTERVAL_S = 0.5


class Stop(Enum):
    CANCEL = "cancel"
    DEADLINE = "deadline"


@dataclass(frozen=True)
class PollPlan:
    forward: bool
    keepalive: bool
    stop: Stop | None


def plan_poll(
    *, pending: bool, now: float, last_activity: float, deadline: float, cancel_requested: bool
) -> PollPlan:
    stop = Stop.CANCEL if cancel_requested else Stop.DEADLINE if now > deadline else None
    return PollPlan(
        forward=pending,
        keepalive=not pending and now - last_activity >= KEEPALIVE_INTERVAL_S,
        stop=stop,
    )
