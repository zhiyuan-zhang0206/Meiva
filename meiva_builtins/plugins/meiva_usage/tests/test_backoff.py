from __future__ import annotations

import pytest

from meiva_builtins.plugins.meiva_usage.backoff import (
    MAX_BACKOFF_S,
    cycle_rate_limited,
    judge_cycle,
    next_interval_s,
)
from meiva_builtins.plugins.meiva_usage.types import CardReading

_BASE_S = 600.0


def test_no_rate_limit_keeps_the_base_interval() -> None:
    assert next_interval_s(_BASE_S, 0) == _BASE_S


def test_each_consecutive_rate_limit_doubles_the_interval() -> None:
    assert [next_interval_s(_BASE_S, n) for n in (1, 2, 3)] == [1200.0, 2400.0, 4800.0]


def test_the_interval_stops_at_120_minutes() -> None:
    assert MAX_BACKOFF_S == 7200.0
    assert next_interval_s(_BASE_S, 4) == 7200.0
    assert next_interval_s(_BASE_S, 5) == 7200.0
    assert next_interval_s(_BASE_S, 10_000) == 7200.0


def test_a_base_interval_above_the_cap_is_never_shortened() -> None:
    assert next_interval_s(10_800.0, 3) == 10_800.0


def test_the_cap_is_a_parameter() -> None:
    assert next_interval_s(60.0, 5, max_backoff_s=300.0) == 300.0


@pytest.mark.parametrize(("base", "count"), [(0.0, 1), (-1.0, 0), (60.0, -1)])
def test_invalid_inputs_fail_fast(base: float, count: int) -> None:
    with pytest.raises(ValueError):
        next_interval_s(base, count)


def test_cycle_rate_limited_is_true_when_any_card_is() -> None:
    ok = CardReading(value="1", detail="", status="ok")
    limited = CardReading(value="—", detail="", status="warn", rate_limited=True)
    assert cycle_rate_limited({"a": ok, "b": limited}) is True
    assert cycle_rate_limited({"a": ok}) is False
    assert cycle_rate_limited({}) is False


def test_judge_cycle_counts_consecutive_limits_and_resets_on_any_other_cycle() -> None:
    ok = {"a": CardReading(value="1", detail="", status="ok")}
    limited = {"a": CardReading(value="—", detail="", status="warn", rate_limited=True)}
    assert judge_cycle(limited, 0) == (1, "rate_limited", "rate_limited")
    assert judge_cycle(limited, 3) == (4, "rate_limited", "rate_limited")
    assert judge_cycle(ok, 3) == (0, "ok", "")
