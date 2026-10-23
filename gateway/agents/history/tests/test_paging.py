# meiva:note
# The windowing rules of a timeline page, asserted on plain items (no store, no renderer).

from dataclasses import dataclass

import pytest

from gateway.agents.history.paging import (
    boundary_read_limit,
    depth_allows,
    initial_window,
    older_segment_available,
    window_before,
    window_or_cross,
)


@dataclass(frozen=True)
class Item:
    item_id: str
    kind: str = "agent_chat"
    source: str | None = None
    impersonation: object | None = None


def ids(items: list[Item]) -> list[str]:
    return [item.item_id for item in items]


# meiva:note
# A prompt, two standing notes and a compact summary, as a segment lays them down.
def standing_head() -> list[Item]:
    return [
        Item("0.0", "system_prompt"),
        Item("1.0", "system_marker", source="memory"),
        Item("2.0", "system_marker", source="timezone"),
        Item("3.0", "inbound_compact_summary"),
    ]


@pytest.mark.parametrize(("depth", "limit"), [(1, 2), (3, 4), (50, 51), (-1, None)])
def test_a_positive_depth_reads_one_boundary_more_than_it_shows(
    depth: int, limit: int | None
) -> None:
    assert boundary_read_limit(depth) == limit


@pytest.mark.parametrize(
    ("rank", "depth", "allowed"),
    [(1, 1, True), (2, 1, False), (7, -1, True), (0, -1, True), (0, 3, False), (3, 3, True)],
)
def test_depth_admits_only_ranks_within_it(rank: int, depth: int, allowed: bool) -> None:
    assert depth_allows(rank, depth) is allowed


def test_an_older_segment_needs_a_boundary_and_depth_for_it() -> None:
    assert older_segment_available(1, 2, -1) is True
    assert older_segment_available(2, 2, -1) is False
    assert older_segment_available(1, 2, 1) is False
    assert older_segment_available(1, 2, 2) is True


def test_window_before_takes_the_items_just_older_than_the_cursor() -> None:
    items = [Item(f"{n}.0") for n in range(1, 6)]
    window, more = window_before(items, "4.0", 2)
    assert (ids(window), more) == (["2.0", "3.0"], True)
    assert window_before(items, "4.0", 9) == (items[:3], False)
    assert window_before(items, "9.9", 2) == ([], False)


def test_a_page_in_the_middle_of_a_segment_does_not_cross() -> None:
    items = [Item(f"{n}.0") for n in range(6)]
    page = window_or_cross(items, "4.0", 2, older_available=False)
    assert (ids(page.items), page.has_more, page.crossed) == (["2.0", "3.0"], True, False)


def test_a_cursor_past_only_standing_context_crosses_to_the_older_segment() -> None:
    items = [*standing_head(), Item("4.0"), Item("5.0")]
    page = window_or_cross(items, "4.0", 2, older_available=True)
    assert (ids(page.items), page.has_more, page.crossed) == (
        ["0.0", "1.0", "2.0", "3.0"],
        True,
        True,
    )
    last = window_or_cross(items, "4.0", 2, older_available=False)
    assert (last.has_more, last.crossed) == (False, True)


def test_a_cursor_that_is_not_in_the_segment_yields_nothing() -> None:
    page = window_or_cross([Item("1.0")], "7.0", 5, older_available=True)
    assert (page.items, page.has_more, page.crossed) == ([], False, False)


def test_untagged_markers_are_not_standing_notes() -> None:
    items = [Item("0.0", "system_prompt"), Item("1.0", "system_marker"), Item("2.0")]
    page = window_or_cross(items, "2.0", 5, older_available=True)
    assert page.crossed is False
    assert ids(page.items) == ["0.0", "1.0"]


def test_the_initial_window_keeps_prompt_notes_and_summaries_ahead_of_the_tail() -> None:
    items = [*standing_head(), *(Item(f"{n}.0") for n in range(4, 12))]
    window, more = initial_window(items, (items[-3:], True), 3, historical_segments_available=False)
    assert ids(window) == ["0.0", "1.0", "2.0", "3.0", "9.0", "10.0", "11.0"]
    assert more is True


def test_the_initial_window_reports_no_more_when_only_standing_context_is_missing() -> None:
    items = [*standing_head(), Item("4.0"), Item("5.0")]
    window, more = initial_window(items, (items[-2:], True), 2, historical_segments_available=False)
    assert ids(window) == ["0.0", "1.0", "2.0", "3.0", "4.0", "5.0"]
    assert more is False


def test_a_window_holding_the_prompt_attaches_nothing_and_keeps_history_visible() -> None:
    items = [Item("0.0", "system_prompt"), Item("1.0")]
    assert initial_window(items, (items, False), 50, historical_segments_available=True) == (
        items,
        True,
    )
