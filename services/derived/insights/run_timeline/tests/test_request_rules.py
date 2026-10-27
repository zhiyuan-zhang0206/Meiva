# meiva:note
# What the run-timeline API accepts and refuses, asserted on the pure rules in `request_rules` and
# the rules in `links`, and the one translation of a refusal into the HTTP response the router owns.

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from services.derived.insights.run_timeline import router as route
from services.derived.insights.run_timeline.links import (
    check_link_content_selector,
    check_link_window,
    parse_agent_ids,
)
from services.derived.insights.run_timeline.request_rules import (
    TimelineRequestError,
    check_message_range,
    check_window_query,
    distinct_in_order,
    message_page_stop,
    positive_agent_id,
    resolve_window,
)

NOON = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
NAIVE = NOON.replace(tzinfo=None)


def refusal[**P](
    call: Callable[P, object], *args: P.args, **kwargs: P.kwargs
) -> TimelineRequestError:
    with pytest.raises(TimelineRequestError) as caught:
        call(*args, **kwargs)
    return caught.value


@pytest.mark.parametrize(
    ("from_", "to", "recent_hours", "detail"),
    [
        (NOON, None, 3, "recent_hours cannot be combined with from/to"),
        (None, NOON, 3, "recent_hours cannot be combined with from/to"),
        (NAIVE, None, None, "from must include a timezone offset"),
        (None, NAIVE, None, "to must include a timezone offset"),
        (NOON, NOON, None, "from must be earlier than to"),
        (NOON, NOON - timedelta(hours=1), None, "from must be earlier than to"),
    ],
)
def test_a_window_query_is_refused_before_any_snapshot_is_read(
    from_: datetime | None, to: datetime | None, recent_hours: int | None, detail: str
) -> None:
    error = refusal(check_window_query, from_, to, recent_hours)

    assert (error.status, error.detail) == (422, detail)


@pytest.mark.parametrize(
    ("from_", "to", "recent_hours"),
    [
        (None, None, None),
        (None, None, 6),
        (NOON, None, None),
        (None, NOON, None),
        (NOON, NOON + timedelta(minutes=1), None),
    ],
)
def test_the_window_queries_the_api_serves_are_accepted(
    from_: datetime | None, to: datetime | None, recent_hours: int | None
) -> None:
    check_window_query(from_, to, recent_hours)


def test_the_zone_of_a_bound_is_not_the_issue_only_that_it_has_one() -> None:
    check_window_query(NOON.astimezone(timezone(timedelta(hours=8))), None, None)


LIFETIME = (NOON - timedelta(days=3), NOON - timedelta(days=1))


def test_an_unbounded_window_is_the_agents_whole_lifetime() -> None:
    assert resolve_window(LIFETIME, None, None, NOON, None) == LIFETIME


def test_an_agent_without_a_lifetime_gets_the_last_day_ending_now() -> None:
    assert resolve_window(None, None, None, NOON, None) == (NOON - timedelta(days=1), NOON)


def test_recent_hours_count_back_from_the_end_but_not_before_the_lifetime_starts() -> None:
    assert resolve_window(LIFETIME, None, None, NOON, 5) == (
        LIFETIME[1] - timedelta(hours=5),
        LIFETIME[1],
    )
    assert resolve_window(LIFETIME, None, None, NOON, 24 * 30) == LIFETIME


def test_a_given_bound_replaces_only_its_own_side() -> None:
    from_ = LIFETIME[0] + timedelta(hours=1)
    to = LIFETIME[1] - timedelta(hours=1)

    assert resolve_window(LIFETIME, from_, None, NOON, None) == (from_, LIFETIME[1])
    assert resolve_window(LIFETIME, None, to, NOON, None) == (LIFETIME[0], to)


def test_a_window_that_the_defaults_leave_empty_or_reversed_is_refused() -> None:
    error = refusal(resolve_window, LIFETIME, LIFETIME[1] + timedelta(hours=1), None, NOON, None)

    assert (error.status, error.detail) == (422, "from must be earlier than to")
    assert refusal(resolve_window, LIFETIME, None, LIFETIME[0], NOON, None).status == 422


def test_a_reversed_message_range_is_refused_and_a_single_message_is_not() -> None:
    assert refusal(check_message_range, 4, 3).detail == "end must not be before start"
    check_message_range(3, 3)


@pytest.mark.parametrize(
    ("start", "end", "limit", "count", "stop"),
    [(0, 4, 50, 10, 4), (0, 9, 3, 10, 2), (5, 9, 5, 10, 9), (5, 9, 4, 10, 8), (7, 7, 1, 8, 7)],
)
def test_a_page_ends_at_the_limit_or_the_requested_end_whichever_comes_first(
    start: int, end: int, limit: int, count: int, stop: int
) -> None:
    assert message_page_stop(start, end, limit, count) == stop


def test_a_range_past_the_history_names_how_long_the_history_is() -> None:
    error = refusal(message_page_stop, 3, 9, 50, 5)

    assert (error.status, error.detail) == (404, "message 9 not found: history has 5")
    assert message_page_stop(3, 4, 50, 5) == 4


def test_the_message_just_past_the_end_of_the_history_is_not_found() -> None:
    error = refusal(message_page_stop, 0, 5, 1, 5)

    assert (error.status, error.detail) == (404, "message 5 not found: history has 5")


def test_the_agents_filter_is_distinct_and_ascending() -> None:
    assert parse_agent_ids("6657,405, 405,12") == [12, 405, 6657]


@pytest.mark.parametrize("agents", ["", "1,,2", "a", "1;2"])
def test_an_agents_filter_that_is_not_integers_is_refused(agents: str) -> None:
    error = refusal(parse_agent_ids, agents)

    assert (error.status, error.detail) == (422, "agents must be comma-separated integers")


def test_the_link_window_needs_zoned_ordered_bounds() -> None:
    zone = refusal(check_link_window, NAIVE, NOON)
    order = refusal(check_link_window, NOON, NOON)

    assert zone.detail == "from and to must include a timezone offset"
    assert order.detail == "from must be earlier than to"
    check_link_window(NOON, NOON + timedelta(seconds=1))


@pytest.mark.parametrize(("inbound_id", "notice_id"), [(None, None), (1, 2)])
def test_exactly_one_link_reference_is_required(
    inbound_id: int | None, notice_id: int | None
) -> None:
    error = refusal(check_link_content_selector, inbound_id, notice_id)

    assert (error.status, error.detail) == (422, "give exactly one of inbound_id and notice_id")
    check_link_content_selector(1, None)
    check_link_content_selector(None, 2)


def test_the_backfill_takes_positive_integer_agent_ids() -> None:
    assert positive_agent_id("405") == 405
    for text in ("0", "-3", "x", "1.5"):
        with pytest.raises(argparse.ArgumentTypeError):
            positive_agent_id(text)


def test_an_agent_named_twice_is_prepared_once_in_the_order_first_named() -> None:
    assert distinct_in_order([6110, 2697, 6110, 2697, 405]) == [6110, 2697, 405]


class _Refusing:
    def __init__(self, refusal: TimelineRequestError) -> None:
        self._refusal = refusal

    def get(self, _agent: int, _snapshot: str | None = None) -> object:
        raise self._refusal


def _request(reader: object) -> Request:
    state = SimpleNamespace(
        timeline_timestamps=SimpleNamespace(clock=SimpleNamespace(now=lambda: NOON)),
        run_timeline_reader=reader,
    )
    return Request({"type": "http", "app": SimpleNamespace(state=state)})


def test_the_router_answers_a_refusal_with_its_status_detail_and_headers() -> None:
    transient = TimelineRequestError(
        503,
        {"code": "timeline_index_queued", "message": "indexing"},
        headers={"Retry-After": "2"},
    )

    with pytest.raises(HTTPException) as caught:
        route.get_run_timeline(_request(_Refusing(transient)), 7, None, None, None, (None, None))

    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "timeline_index_queued", "message": "indexing"}
    assert caught.value.headers == {"Retry-After": "2"}


def test_a_bad_window_query_is_refused_before_the_snapshot_is_read() -> None:
    unreachable = TimelineRequestError(503, {"code": "timeline_index_queued", "message": "later"})

    with pytest.raises(HTTPException) as caught:
        route.get_run_timeline(_request(_Refusing(unreachable)), 7, NOON, None, 3, (None, None))

    assert caught.value.status_code == 422
    assert caught.value.detail == "recent_hours cannot be combined with from/to"


def test_the_links_route_answers_a_bad_agents_filter_before_a_bad_window() -> None:
    with pytest.raises(HTTPException) as caught:
        route.get_run_timeline_links(_request(object()), "1,x", NAIVE, NOON)

    assert (caught.value.status_code, caught.value.detail) == (
        422,
        "agents must be comma-separated integers",
    )


def test_the_links_route_answers_a_window_without_a_zone() -> None:
    with pytest.raises(HTTPException) as caught:
        route.get_run_timeline_links(_request(object()), "1,2", NAIVE, NOON)

    assert (caught.value.status_code, caught.value.detail) == (
        422,
        "from and to must include a timezone offset",
    )


def test_the_context_route_answers_a_refusal_of_the_reader_as_http() -> None:
    class Missing:
        def get(self, _agent: int, _snapshot: str | None = None) -> object:
            return SimpleNamespace(sessions=1)

        def request(self, _cut: object, _at: int) -> object:
            raise TimelineRequestError(404, "the agent has made no LLM request")

    state = _request(Missing()).scope["app"].state
    state.insights_config = SimpleNamespace(default_model="claude-opus-5")
    request = Request({"type": "http", "app": SimpleNamespace(state=state)})

    with pytest.raises(HTTPException) as caught:
        route.get_run_timeline_context(request, 7, 0, None)

    assert (caught.value.status_code, caught.value.detail) == (
        404,
        "the agent has made no LLM request",
    )
