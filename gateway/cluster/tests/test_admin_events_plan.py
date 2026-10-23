# meiva:note
# The admin events request: filters in, the exact event query out (or a refusal).

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from gateway.cluster.admin_events_plan import (
    AdminEventsError,
    AdminEventsFilters,
    AdminEventsQuery,
    plan_admin_events,
)

_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _plan(default_limit: int = 200, **filters: object) -> AdminEventsQuery:
    base: dict[str, object] = {
        "agent_id": None,
        "service_only": False,
        "level": None,
        "since": None,
        "event": None,
        "grep": None,
        "limit": None,
    }
    return plan_admin_events(
        # meiva:note
        # The keyword arguments are spread from a dict of changed fields, which pyright cannot match to the signature.
        AdminEventsFilters(**{**base, **filters}),  # pyright: ignore[reportArgumentType]
        default_limit=default_limit,
        now=_NOW,
    )


def test_no_filters_reads_telemetry_and_log_events_of_the_last_day() -> None:
    query = _plan()

    assert query == AdminEventsQuery(
        agent_id=None,
        service_only=False,
        categories=("telemetry", "log"),
        event_names=None,
        level_min=None,
        grep=None,
        from_=_NOW - timedelta(hours=24),
        limit=200,
    )


def test_the_default_limit_is_the_configured_one_and_an_explicit_limit_wins() -> None:
    assert _plan(default_limit=7).limit == 7
    assert _plan(default_limit=7, limit=50).limit == 50


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_a_limit_outside_one_to_a_thousand_is_refused(limit: int) -> None:
    with pytest.raises(AdminEventsError, match="limit must be in"):
        _plan(limit=limit)


def test_the_configured_default_is_held_to_the_same_range() -> None:
    with pytest.raises(AdminEventsError, match="limit must be in"):
        _plan(default_limit=5000)


def test_agent_id_and_service_only_exclude_each_other() -> None:
    assert _plan(agent_id=3).agent_id == 3
    assert _plan(service_only=True).service_only is True
    with pytest.raises(AdminEventsError, match="not both"):
        _plan(agent_id=1, service_only=True)


def test_level_is_a_minimum_in_lower_case() -> None:
    assert _plan(level="WARNING").level_min == "warning"
    assert _plan(level="error").level_min == "error"
    assert _plan(level="").level_min is None
    with pytest.raises(AdminEventsError, match="unknown level"):
        _plan(level="NOPE")


@pytest.mark.parametrize(
    ("since", "seconds_before_now"),
    [("1m", 60), ("2h", 7200), ("30s", 30), ("1.5d", 129600)],
)
def test_since_accepts_a_relative_window(since: str, seconds_before_now: float) -> None:
    assert _plan(since=since).from_ == _NOW - timedelta(seconds=seconds_before_now)


def test_since_accepts_an_absolute_timestamp() -> None:
    instant = _NOW - timedelta(hours=5)
    plus_two = timezone(timedelta(hours=2))
    assert _plan(since=instant.isoformat().replace("+00:00", "Z")).from_ == instant
    assert _plan(since=instant.astimezone(plus_two).isoformat()).from_ == instant


@pytest.mark.parametrize("since", ["garbage", "xm", "  "])
def test_since_that_is_neither_is_refused(since: str) -> None:
    with pytest.raises(AdminEventsError, match="since"):
        _plan(since=since)


def test_event_names_split_on_commas_and_drop_blanks() -> None:
    assert _plan(event="spawn, terminate,,").event_names == ("spawn", "terminate")


def test_grep_is_passed_through() -> None:
    assert _plan(grep="hello").grep == "hello"
