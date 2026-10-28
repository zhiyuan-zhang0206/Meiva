# meiva:note
# The arithmetic of a stop's shared deadline, as functions of values.
#
# A stop spends one monotonic deadline across its phases (`base.clock.Deadline` carries it and
# what is left of it). The shell that reads the clock (`stop_waits`, the unit stop's
# shell-session closure) asks these functions what the reading means: how long to sleep before
# observing again, how long a closure grace may be. Nothing here reads the clock or sleeps, so a
# test passes the numbers it wants.

from __future__ import annotations

import math

__all__ = [
    "closure_grace_s",
    "poll_delay",
    "postgres_fast_budget_s",
    "require_valid_timeout",
    "settle_until",
]


# meiva:note
# The stop's timeout, or a refusal: a deadline needs a finite, positive span.
def require_valid_timeout(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("maintenance stop timeout must be finite and positive")
    return timeout


# meiva:note
# How long a bounded wait sleeps before it observes again, or None once its time is spent.
#
# A wait observes first and asks this afterwards, so a condition that already holds settles
# even when the budget is spent; the last sleep never overruns the deadline.
def poll_delay(left_s: float | None, interval_s: float) -> float | None:
    return None if left_s is None else min(interval_s, left_s)


# meiva:note
# The grace a shell-session closure gives its HUP/TERM leg: what is left of the stop's
# time, up to the ceiling, never negative (the SIGKILL leg has its own bound).
def closure_grace_s(left_s: float, ceiling_s: float) -> float:
    return max(0.0, min(left_s, ceiling_s))


# meiva:note
# When the closure evidence stops waiting: the stop's deadline, and at least `floor_s`
# from `now`, so a spent deadline still lets a session finish tearing down.
def settle_until(deadline: float, now: float, floor_s: float) -> float:
    return max(deadline, now + floor_s)


# meiva:note
# How long Postgres' fast shutdown may take: what is left minus the legs after it.
#
# A stop whose remaining time is not larger than the reserve gives the fast shutdown all of it.
def postgres_fast_budget_s(left_s: float, reserve_s: float) -> float:
    return left_s - reserve_s if left_s > reserve_s else left_s
