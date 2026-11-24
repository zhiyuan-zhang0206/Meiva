# meiva:note
# The resident loops of the events-maintenance service and how long each may go without completing
# work before the health endpoint reads it as wedged.
#
# The dispatch and resolution loops run blocking passes under the deadlines the cluster configures.
# The registry gauge is one single-row read per minute, so it is wedged after three missed intervals.
from __future__ import annotations

from dataclasses import dataclass

REGISTRY_GAUGE_DEADLINE_S = 180.0


@dataclass(frozen=True)
class LoopSpec:
    name: str
    deadline_s: float


def roster(*, dispatch_deadline_s: float) -> tuple[LoopSpec, ...]:
    return (
        LoopSpec("dispatch", dispatch_deadline_s),
        LoopSpec("registry_gauge", REGISTRY_GAUGE_DEADLINE_S),
    )
