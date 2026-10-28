# meiva:note
# The arithmetic of a stop's shared deadline: functions of numbers, no clock and no sleep.

from __future__ import annotations

import math

import pytest

from cli.commands.data_plane.stop_budget import (
    closure_grace_s,
    poll_delay,
    postgres_fast_budget_s,
    require_valid_timeout,
    settle_until,
)


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, -math.inf])
def test_a_deadline_needs_a_finite_positive_span(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        require_valid_timeout(timeout)


def test_a_finite_positive_span_is_its_own_deadline_budget() -> None:
    assert require_valid_timeout(0.001) == 0.001


@pytest.mark.parametrize(
    ("left", "interval", "delay"),
    [(6.0, 0.05, 0.05), (0.01, 0.05, 0.01), (None, 0.05, None)],
)
def test_a_wait_sleeps_one_interval_never_past_its_deadline_and_stops_when_it_is_spent(
    left: float | None, interval: float, delay: float | None
) -> None:
    assert poll_delay(left, interval) == delay


@pytest.mark.parametrize(
    ("left", "ceiling", "grace"),
    [(3.0, 10.0, 3.0), (100.0, 10.0, 10.0), (-1.0, 10.0, 0.0), (0.0, 10.0, 0.0)],
)
def test_the_closure_grace_is_the_stops_remaining_time_up_to_its_ceiling(
    left: float, ceiling: float, grace: float
) -> None:
    assert closure_grace_s(left, ceiling) == grace


@pytest.mark.parametrize(
    ("deadline", "now", "until"),
    [(60.0, 10.0, 60.0), (10.0, 10.0, 13.0), (5.0, 10.0, 13.0)],
)
def test_the_closure_evidence_waits_to_the_deadline_and_at_least_the_kill_legs_bound(
    deadline: float, now: float, until: float
) -> None:
    assert settle_until(deadline, now, 3.0) == until


@pytest.mark.parametrize(
    ("left", "reserve", "budget"),
    [(100.0, 23.0, 77.0), (23.0, 23.0, 23.0), (5.0, 23.0, 5.0)],
)
def test_postgres_fast_shutdown_leaves_room_for_the_legs_after_it(
    left: float, reserve: float, budget: float
) -> None:
    # meiva:note
    # A stop whose remaining time is not larger than the reserve gives the fast shutdown all of it.
    assert postgres_fast_budget_s(left, reserve) == budget
