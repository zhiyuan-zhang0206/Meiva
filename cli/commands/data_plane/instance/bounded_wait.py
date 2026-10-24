# meiva:note
# What to do next while polling a condition that has a deadline.
#
# Every bounded wait in the data-plane bring-up (the reachable bind address, the loaded
# pg_hba, a daemonized Redis answering) observes first and asks this function afterwards,
# so a condition that already holds settles even when the budget is spent. The loops own
# the clock and the sleeping; the verdict is a value.

from __future__ import annotations

from enum import Enum

__all__ = ["Wait", "wait_verdict"]


# meiva:note
# What one observation of a bounded wait means.
class Wait(Enum):
    DONE = "done"
    RETRY = "retry"
    EXPIRED = "expired"


# meiva:note
# Settle on a satisfied condition, give up once `elapsed_s` reaches `timeout_s`, else retry.
def wait_verdict(*, satisfied: bool, elapsed_s: float, timeout_s: float) -> Wait:
    if satisfied:
        return Wait.DONE
    if elapsed_s >= timeout_s:
        return Wait.EXPIRED
    return Wait.RETRY
