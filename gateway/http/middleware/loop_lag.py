# meiva:note
# What one tick of the event-loop lag monitor decides: values in, the next window out.
#
# The monitor (`gateway.http.middleware.runtime_metrics`) schedules a callback and, when it
# runs, hands this module the time it ran and the time it was due. The lag is the difference;
# the worst lag and the slow ticks accumulate in a window that closes, is reported and restarts
# once the emit interval has passed.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["LagPolicy", "LagReport", "LagWindow", "TickPlan", "tick_plan"]


# meiva:note
# How often the loop is sampled, when a tick counts as slow and how often a window closes.
@dataclass(frozen=True)
class LagPolicy:
    tick_interval_s: float
    emit_interval_s: float
    slow_tick_threshold_ms: float


# meiva:note
# The worst lag and the slow ticks seen since the last report.
@dataclass(frozen=True)
class LagWindow:
    last_emit_at: float
    max_lag_ms: float = 0.0
    slow_ticks: int = 0


# meiva:note
# A closed window, to be emitted.
@dataclass(frozen=True)
class LagReport:
    lag_ms: float
    slow_ticks: int


# meiva:note
# The window after this tick, the report it closes (if any) and when the next tick is due.
@dataclass(frozen=True)
class TickPlan:
    window: LagWindow
    report: LagReport | None
    next_at: float


# meiva:note
# The plan for a tick that ran at `now` for a callback due at `expected_at`.
def tick_plan(window: LagWindow, policy: LagPolicy, *, now: float, expected_at: float) -> TickPlan:
    lag_ms = max(0.0, (now - expected_at) * 1000.0)
    grown = LagWindow(
        last_emit_at=window.last_emit_at,
        max_lag_ms=max(window.max_lag_ms, lag_ms),
        slow_ticks=window.slow_ticks + (1 if lag_ms >= policy.slow_tick_threshold_ms else 0),
    )
    next_at = now + policy.tick_interval_s
    if now - window.last_emit_at < policy.emit_interval_s:
        return TickPlan(grown, None, next_at)
    return TickPlan(
        LagWindow(last_emit_at=now),
        LagReport(lag_ms=round(grown.max_lag_ms, 1), slow_ticks=grown.slow_ticks),
        next_at,
    )
