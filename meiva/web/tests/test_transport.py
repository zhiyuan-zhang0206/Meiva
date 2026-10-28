# meiva:note
# The pacer's waits, on a clock the caller supplies. `pacing.py` proves the arithmetic; this
# proves the transport spends exactly those waits on the clock it was built with and on nothing
# else, so a batch's spacing is a value a test drives instead of real seconds.

from __future__ import annotations

import pytest

from base.clock.tests.manual_timers import ManualTimers
from meiva.web.transport import RateLimiter


# meiva:note
# Read through the process timers instead, these calls would sleep for real and the manual clock
# would record nothing.
def test_the_rate_limiter_waits_on_the_clock_it_was_given() -> None:
    timers = ManualTimers()
    limiter = RateLimiter(5.0, timers=timers)

    for _ in range(3):
        limiter.acquire()

    assert timers.sleeps == pytest.approx([0.2, 0.2])
    assert timers.monotonic() == pytest.approx(0.4)


# meiva:note
# A zero or negative qps does not pace, so the limiter never asks its clock for the time.
def test_an_unpaced_limiter_never_reads_its_clock() -> None:
    class _UnreadTimers(ManualTimers):
        def monotonic(self) -> float:
            raise AssertionError("an unpaced limiter must not read the clock")

        def sleep(self, seconds: float) -> None:
            raise AssertionError("an unpaced limiter must not sleep")

    RateLimiter(0.0, timers=_UnreadTimers()).acquire()
