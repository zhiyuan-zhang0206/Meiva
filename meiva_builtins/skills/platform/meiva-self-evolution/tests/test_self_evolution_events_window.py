# meiva:note
# The decisions of reading `/api/events` over a window, over values only.

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script

ew = cast(Any, load_skill_script("platform", "meiva-self-evolution", "scripts", "events_window.py"))
_START = datetime(2026, 8, 13, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return _START + timedelta(seconds=seconds)


def test_page_params_are_offset_free_and_filter_by_agent_only_when_asked() -> None:
    plain = ew.page_params("telemetry", _at(0), _at(60), None)
    scoped = ew.page_params("audit", _at(0), _at(60), 42)

    assert plain == {
        "category": "telemetry",
        "from": "2026-08-13T00:00:00+00:00",
        "to": "2026-08-13T00:01:00+00:00",
        "limit": 1000,
        "offset": 0,
    }
    assert scoped["agent_id"] == 42
    assert "agent_id" not in plain


def test_an_empty_page_with_nothing_more_is_an_empty_slice() -> None:
    assert ew.slice_step([], {"has_more": False}, _at(0), _at(60)) == ew.Take([], False)


def test_a_page_that_fits_is_taken_as_it_is() -> None:
    rows = [{"id": 1}]

    assert ew.slice_step(rows, {"has_more": False}, _at(0), _at(60)) == ew.Take(rows, False)


def test_an_overflowing_slice_is_split_at_its_midpoint() -> None:
    step = ew.slice_step([{"id": 1}], {"has_more": True}, _at(0), _at(60))

    assert step == ew.Split(_at(30))


def test_a_minimal_overflowing_slice_is_taken_and_marked_truncated() -> None:
    rows = [{"id": 1}]

    assert ew.slice_step(rows, {"has_more": True}, _at(0), _at(1)) == ew.Take(rows, True)


def test_a_missing_has_more_raises_instead_of_truncating() -> None:
    with pytest.raises(KeyError):
        ew.slice_step([{"id": 1}], {}, _at(0), _at(60))


def test_merge_slices_dedupes_by_id_and_sorts_oldest_first() -> None:
    first = [{"id": 2, "ts": "b"}, {"id": 1, "ts": "a"}]
    second = [{"id": 2, "ts": "b"}, {"id": 3, "ts": "c"}, {"ts": "a2"}, {"ts": "a2"}]

    merged = ew.merge_slices([first, second])

    assert [row.get("id") for row in merged] == [1, None, None, 2, 3]
    assert [row["ts"] for row in merged] == ["a", "a2", "a2", "b", "c"]


def test_http_failure_detail_bounds_every_field() -> None:
    detail = ew.http_failure_detail(503, "Service Unavailable", {"x": "h" * 1000}, "b" * 1000)

    assert detail.startswith("HTTP 503 Service Unavailable; headers: ")
    assert detail.count("(+") == 2
    assert "b" * 300 in detail and "b" * 301 not in detail


def test_window_ends_at_to_and_starts_days_before_unless_told() -> None:
    now = _at(86400 * 3)

    assert ew.resolve_window(now, 2, None, None) == ew.Window(_at(86400), now)
    assert ew.resolve_window(now, 2, _at(5), None) == ew.Window(_at(5), now)
    assert ew.resolve_window(now, 2, None, _at(86400 * 2)) == ew.Window(_at(0), _at(86400 * 2))


def test_an_empty_or_inverted_window_is_refused() -> None:
    with pytest.raises(ValueError, match="empty window"):
        ew.Window(_at(10), _at(10))
    with pytest.raises(ValueError, match="empty window"):
        ew.resolve_window(_at(100), 1, _at(200), None)


def test_week_label_is_the_utc_date() -> None:
    late_in_new_york = datetime(2026, 8, 13, 22, 30, tzinfo=UTC).astimezone(
        timezone(timedelta(hours=-5))
    )
    next_day_in_tokyo = datetime(2026, 8, 13, 23, 59, tzinfo=UTC).astimezone(
        timezone(timedelta(hours=9))
    )

    assert ew.week_label(late_in_new_york) == "2026-08-13"
    assert ew.week_label(next_day_in_tokyo) == "2026-08-13"
