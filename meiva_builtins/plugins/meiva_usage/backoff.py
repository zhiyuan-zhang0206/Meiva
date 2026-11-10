# meiva:note
# Pure pacing decisions for a collector loop: how long to sleep after a cycle, and whether the
# cycle was rate limited. Data in, decisions out: the loop reads the clock and sleeps, this module
# only computes.
# meiva:link meiva_builtins/plugins/meiva_usage/README.md

from __future__ import annotations

from collections.abc import Mapping

from meiva_builtins.plugins.meiva_usage.types import CardReading

# meiva:note
# The longest a rate-limited collector waits between cycles. A provider that keeps answering 429
# is asked at most once per two hours.
MAX_BACKOFF_S: float = 120.0 * 60.0

# meiva:note
# A shift of this many doublings already exceeds any base interval's cap, so larger counts add
# nothing; the clamp only keeps the arithmetic from overflowing on a long-running loop.
_MAX_DOUBLINGS = 20


# meiva:note
# True when any card of the cycle carries a rate-limited reading. One 429 poll feeds every card of
# its collector, so any is enough.
def cycle_rate_limited(readings: Mapping[str, CardReading]) -> bool:
    return any(reading.rate_limited for reading in readings.values())


# meiva:note
# What a finished cycle means for the loop: the new count of consecutive rate-limited cycles (reset
# by any cycle that was not limited) and the `outcome` / `reason` its cycle event carries.
def judge_cycle(
    readings: Mapping[str, CardReading], consecutive_rate_limits: int
) -> tuple[int, str, str]:
    if cycle_rate_limited(readings):
        return consecutive_rate_limits + 1, "rate_limited", "rate_limited"
    return 0, "ok", ""


# meiva:note
# Seconds to sleep before the next cycle: the base interval doubled once per consecutive rate-limited
# cycle, capped at `MAX_BACKOFF_S`. Zero consecutive limits is the base interval itself, and an
# interval already above the cap is never shortened by backing off.
def next_interval_s(
    base_interval_s: float,
    consecutive_rate_limits: int,
    *,
    max_backoff_s: float = MAX_BACKOFF_S,
) -> float:
    if base_interval_s <= 0:
        raise ValueError(f"base_interval_s must be positive, got {base_interval_s}")
    if consecutive_rate_limits < 0:
        raise ValueError(
            f"consecutive_rate_limits must not be negative, got {consecutive_rate_limits}"
        )
    if consecutive_rate_limits == 0:
        return base_interval_s
    doublings = min(consecutive_rate_limits, _MAX_DOUBLINGS)
    return max(base_interval_s, min(base_interval_s * 2.0**doublings, max_backoff_s))
