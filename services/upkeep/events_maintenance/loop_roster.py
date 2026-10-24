# meiva:note
# The resident loops of the events-maintenance service and how long each may go without completing
# work before the health endpoint reads it as wedged.
#
# The rollup and resolution loops run blocking passes under the deadlines the cluster configures.
# The registry gauge is one single-row read per minute and the alert reconciliation is one Grafana
# read (10 s timeout) and one bounded statement per five minutes, so both are wedged after three
# missed intervals. The reconciliation exists only on a unit that holds the Grafana credential.
from __future__ import annotations

from dataclasses import dataclass

REGISTRY_GAUGE_DEADLINE_S = 180.0
ALERT_RECONCILIATION_DEADLINE_S = 180.0


@dataclass(frozen=True)
class LoopSpec:
    name: str
    deadline_s: float


def roster(
    *, dispatch_deadline_s: float, resolution_deadline_s: float, reconcile_alerts: bool
) -> tuple[LoopSpec, ...]:
    loops = (
        LoopSpec("dispatch", dispatch_deadline_s),
        LoopSpec("resolution", resolution_deadline_s),
        LoopSpec("registry_gauge", REGISTRY_GAUGE_DEADLINE_S),
    )
    if reconcile_alerts:
        return (*loops, LoopSpec("alert_reconciliation", ALERT_RECONCILIATION_DEADLINE_S))
    return loops
