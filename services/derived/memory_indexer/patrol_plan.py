# meiva:note
# What the patrol loop does after a pass: when the next pass is due and what to say about it.
#
# Values in, a verdict out, so the loop (`daemon.patrol_loop`) only executes it: it has no
# decision of its own between a finished pass and the next wait. `retry_delay_s` is the
# doubling back-off of an incomplete pass, capped at the patrol interval. `liveness_ceiling_s`
# is how stale the loop's heartbeat may grow before the daemon counts as wedged.

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "LIVENESS_SAFETY_MARGIN_S",
    "LIVENESS_TIMEOUT_FLOOR_S",
    "NextPatrol",
    "liveness_ceiling_s",
    "next_patrol",
    "retry_delay_s",
]

# meiva:note
# The ceiling is derived from one provider batch's full retry budget: a single legitimate call
# can exceed 180s, and several shorter calls can compound. The pass beats before each provider
# and backend call, including commits and deletes, so calls cannot compound in one gap (default
# batch budget 606s). A false kill costs a rebuild; later true-wedge detection costs staleness
# only, since search keeps reading the existing index. The floor is the historic ceiling and
# preserves the other loop branches' slack; the margin covers executor scheduling, local
# processing and loop resumption. Commit calls beat separately; NumPy's 300s upsert allowance
# fits the default 636s.
LIVENESS_TIMEOUT_FLOOR_S = 180.0
LIVENESS_SAFETY_MARGIN_S = 30.0


# meiva:note
# The longest stale heartbeat a healthy loop may show, given one provider batch's retry budget.
def liveness_ceiling_s(batch_budget_s: float) -> float:
    return max(LIVENESS_TIMEOUT_FLOOR_S, batch_budget_s + LIVENESS_SAFETY_MARGIN_S)


# meiva:note
# The back-off after `failures` consecutive incomplete passes: the first retry waits
# `retry_backoff_s`, each further one doubles it, never past `patrol_interval_s`.
def retry_delay_s(*, retry_backoff_s: float, patrol_interval_s: float, failures: int) -> float:
    delay = retry_backoff_s
    for _ in range(failures - 1):
        delay = min(delay * 2, patrol_interval_s)
    return delay


# meiva:note
# When the next pass is due (`delay_s` after the pass finished) and why: `complete` (a clean
# pass, the normal cadence), `retry` (an incomplete pass, backed off) or `over_limit` (too many
# incomplete passes in a row, back to the normal cadence).
@dataclass(frozen=True)
class NextPatrol:
    outcome: Literal["complete", "retry", "over_limit"]
    delay_s: float


# meiva:note
# The verdict for a finished pass. `failures` counts consecutive incomplete passes including
# this one (0 after a clean pass); `over_limit` is whether that count passed the retry limit.
def next_patrol(
    *,
    complete: bool,
    failures: int,
    over_limit: bool,
    retry_backoff_s: float,
    patrol_interval_s: float,
) -> NextPatrol:
    if complete:
        return NextPatrol("complete", patrol_interval_s)
    if over_limit:
        return NextPatrol("over_limit", patrol_interval_s)
    return NextPatrol(
        "retry",
        retry_delay_s(
            retry_backoff_s=retry_backoff_s, patrol_interval_s=patrol_interval_s, failures=failures
        ),
    )
