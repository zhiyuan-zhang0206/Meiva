# meiva:note
# How a `GET /api/events` request becomes a store read.
#
# Every case passes the request values and a `now`, and asserts the plan. The route, the stores
# and the wire shape are exercised against real rows in `gateway/tests/events`.

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from gateway.events.events_plan import (
    EventsRequest,
    InvalidEventsQueryError,
    page_depth,
    plan_events,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

BARE = EventsRequest(
    category=None,
    event_name=None,
    agent_id=None,
    trace_id=None,
    machine=None,
    level=None,
    tier=None,
    from_=None,
    to=None,
    hours=None,
    limit=None,
    offset=0,
    with_total=False,
)


def plan(**changes: object):
    return plan_events(replace(BARE, **changes), now=NOW, default_limit=100)


def test_a_bare_request_reads_both_stores_for_the_last_day() -> None:
    result = plan()
    assert (result.use_audit, result.use_telemetry) == (True, True)
    assert result.telemetry_categories == ["telemetry", "log"]
    assert result.filters.from_ == NOW - timedelta(hours=24)
    assert result.filters.to is None
    assert (result.limit, result.offset, result.with_total) == (100, 0, False)


@pytest.mark.parametrize(
    ("category", "audit", "telemetry"),
    [("audit", True, False), ("telemetry", False, True), ("log", False, True)],
)
def test_a_category_picks_one_store(category: str, audit: bool, telemetry: bool) -> None:
    result = plan(category=category)
    assert (result.use_audit, result.use_telemetry) == (audit, telemetry)
    if telemetry:
        assert result.telemetry_categories == [category]


def test_a_registered_event_name_picks_the_store_its_category_lives_in() -> None:
    assert (plan(event_name="spawn").use_audit, plan(event_name="spawn").use_telemetry) == (
        True,
        False,
    )
    assert (
        plan(event_name="llm_usage").use_audit,
        plan(event_name="llm_usage").use_telemetry,
    ) == (False, True)
    unknown = plan(event_name="does_not_exist")
    assert (unknown.use_audit, unknown.use_telemetry) == (True, True)


def test_filters_are_normalized() -> None:
    result = plan(
        event_name="llm_usage",
        agent_id=42,
        machine="machine-1",
        level="WARNING",
        trace_id="ABCDEF",
        tier="business, anomaly,business",
    )
    assert result.filters.event_names == ["llm_usage"]
    assert result.filters.agent_id == 42
    assert result.filters.machine == "machine-1"
    assert result.filters.level == "warning"
    assert result.filters.trace_id == "abcdef"
    assert result.filters.tiers == ["business", "anomaly"]


def test_an_explicit_window_is_passed_through() -> None:
    result = plan(from_=datetime(2026, 8, 1, tzinfo=UTC), to=datetime(2026, 8, 2, tzinfo=UTC))
    assert result.filters.from_ == datetime(2026, 8, 1, tzinfo=UTC)
    assert result.filters.to == datetime(2026, 8, 2, tzinfo=UTC)


def test_hours_is_a_window_ending_now() -> None:
    assert plan(hours=2).filters.from_ == NOW - timedelta(hours=2)


def test_the_page_is_the_requested_one_or_the_configured_default() -> None:
    assert (plan(limit=5, offset=10).limit, plan(limit=5, offset=10).offset) == (5, 10)
    assert plan_events(BARE, now=NOW, default_limit=7).limit == 7


def test_with_total_is_carried() -> None:
    assert plan(with_total=True).with_total is True


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"category": "bogus"}, "category must be one of"),
        ({"level": "fatal"}, "level must be one of"),
        ({"tier": "business,urgent"}, "tier must be a comma-separated list"),
        # meiva:note
        # A naive datetime on purpose: the plan under test refuses it.
        ({"from_": datetime(2026, 8, 4)}, "from must include a timezone offset"),  # noqa: DTZ001
        ({"to": datetime(2026, 8, 4)}, "to must include a timezone offset"),  # noqa: DTZ001
        (
            {"from_": datetime(2026, 8, 4, tzinfo=UTC), "hours": 1.0},
            "mutually exclusive",
        ),
    ],
)
def test_an_illegal_request_is_refused_with_its_reason(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(InvalidEventsQueryError, match=message):
        plan(**changes)


def test_one_store_pages_directly_and_two_stores_each_give_the_newest_rows() -> None:
    assert page_depth(plan(category="telemetry", limit=5, offset=10)) == (5, 10)
    assert page_depth(plan(category="audit", limit=5, offset=10)) == (5, 10)
    assert page_depth(plan(limit=5, offset=10)) == (15, 0)
