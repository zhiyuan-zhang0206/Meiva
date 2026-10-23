# meiva:note
# One machine's last status_probe outcome, and what it is worth at a given time.
#
# The heartbeat service's liveness pass probes every roster-visible agent-runner once a
# minute and writes the outcome into `machine_status_snapshot`
# (`services/wake/heartbeat/liveness.py`); `gateway.cluster.roster.snapshots` reads those rows. A row
# older than `MAX_AGE_S` (the heartbeat service is down or wedged), or no row, is not evidence of
# anything. Every verdict takes `now`: the reader decides what time it is, this module only
# compares.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from base.agents.observation.evidence import (
    LIVENESS_PASS_INTERVAL_S,
    MACHINE_OFFLINE_AFTER_FAILURES,
)

__all__ = ["MAX_AGE_S", "Snapshot"]

# meiva:note
# Three liveness-pass intervals: one missed pass plus the pass that rewrites the row.
MAX_AGE_S = 3 * LIVENESS_PASS_INTERVAL_S


# meiva:note
# One machine's last status_probe outcome.
@dataclass(frozen=True)
class Snapshot:
    observed_at: datetime
    reachable: bool
    consecutive_failures: int
    # meiva:note
    # The last ClusterStatus payload (kept across a failed attempt); None when the
    # last reachable answer did not validate as ClusterStatus, or none ever arrived.
    status: dict[str, Any] | None
    status_at: datetime | None

    # meiva:note
    # Whether the heartbeat liveness pass has probed this machine recently.
    def fresh(self, now: datetime) -> bool:
        return (now - self.observed_at).total_seconds() <= MAX_AGE_S

    # meiva:note
    # Whether a recent probe pass failed it often enough to call it offline —
    # the same two-consecutive-failures grading the agent liveness uses.
    def known_down(self, now: datetime) -> bool:
        return self.fresh(now) and self.consecutive_failures >= MACHINE_OFFLINE_AFTER_FAILURES

    # meiva:note
    # Whether the passes failed often enough, or never answered, to read offline.
    def failed_enough(self) -> bool:
        return not self.reachable and (
            self.status is None or self.consecutive_failures >= MACHINE_OFFLINE_AFTER_FAILURES
        )
