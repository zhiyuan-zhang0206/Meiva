# meiva:note
# One tick of the event-loop lag monitor: the window, the report and the next due time.

from __future__ import annotations

import pytest

from gateway.http.middleware.loop_lag import (
    LagPolicy,
    LagReport,
    LagWindow,
    TickPlan,
    tick_plan,
)

_POLICY = LagPolicy(tick_interval_s=1.0, emit_interval_s=60.0, slow_tick_threshold_ms=100.0)


def test_a_tick_on_time_grows_nothing_and_schedules_the_next_one() -> None:
    plan = tick_plan(LagWindow(last_emit_at=0.0), _POLICY, now=1.0, expected_at=1.0)
    assert plan == TickPlan(LagWindow(last_emit_at=0.0), None, 2.0)


def test_a_late_tick_keeps_the_worst_lag_and_counts_a_slow_tick() -> None:
    window = LagWindow(last_emit_at=0.0, max_lag_ms=40.0, slow_ticks=1)
    plan = tick_plan(window, _POLICY, now=1.25, expected_at=1.0)
    assert plan == TickPlan(LagWindow(last_emit_at=0.0, max_lag_ms=250.0, slow_ticks=2), None, 2.25)


def test_a_tick_below_the_threshold_is_not_slow() -> None:
    plan = tick_plan(LagWindow(last_emit_at=0.0), _POLICY, now=1.05, expected_at=1.0)
    assert plan.window.slow_ticks == 0
    assert plan.window.max_lag_ms == pytest.approx(50.0)


def test_an_early_tick_has_no_lag() -> None:
    plan = tick_plan(LagWindow(last_emit_at=0.0), _POLICY, now=0.9, expected_at=1.0)
    assert plan.window.max_lag_ms == 0.0


def test_the_window_closes_with_this_ticks_lag_and_restarts_at_now() -> None:
    window = LagWindow(last_emit_at=0.0, max_lag_ms=120.0, slow_ticks=1)
    plan = tick_plan(window, _POLICY, now=60.0, expected_at=59.8)
    assert plan.report == LagReport(lag_ms=200.0, slow_ticks=2)
    assert plan.window == LagWindow(last_emit_at=60.0)
    assert plan.next_at == 61.0


def test_the_lag_is_reported_rounded_to_a_tenth_of_a_millisecond() -> None:
    plan = tick_plan(LagWindow(last_emit_at=0.0), _POLICY, now=60.0, expected_at=59.9996)
    assert plan.report == LagReport(lag_ms=0.4, slow_ticks=0)
