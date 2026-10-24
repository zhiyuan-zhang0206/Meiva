# meiva:note
# The windowing rules of a timeline page, asserted on plain rows (no store, no renderer).

import pytest

from gateway.agents.history.paging import (
    Row,
    Selection,
    boundary_read_limit,
    depth_allows,
    initial_window,
    older_segment_available,
    window_before,
    window_or_cross,
)


def row(item_id: str, kind: str = "agent_chat", *, tagged: bool = False) -> Row:
    return Row(item_id, kind, tagged_plain=tagged)


def picked(rows: list[Row], selection: Selection) -> list[str]:
    return [rows[i].item_id for i in selection.indices]


# meiva:note
# A prompt, two standing notes and a compact summary, as a segment lays them down.
def standing_head() -> list[Row]:
    return [
        row("0.0", "system_prompt"),
        row("1.0", "system_marker", tagged=True),
        row("2.0", "system_marker", tagged=True),
        row("3.0", "inbound_compact_summary"),
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
    rows = [row(f"{n}.0") for n in range(1, 6)]
    window = window_before(rows, "4.0", 2)
    assert (picked(rows, window), window.has_more) == (["2.0", "3.0"], True)
    everything = window_before(rows, "4.0", 9)
    assert (picked(rows, everything), everything.has_more) == (["1.0", "2.0", "3.0"], False)
    missing = window_before(rows, "9.9", 2)
    assert (missing.indices, missing.has_more) == ((), False)


def test_a_page_in_the_middle_of_a_segment_does_not_cross() -> None:
    rows = [row(f"{n}.0") for n in range(6)]
    page = window_or_cross(rows, "4.0", 2, older_available=False)
    assert (picked(rows, page), page.has_more, page.crossed) == (["2.0", "3.0"], True, False)


def test_a_cursor_past_only_standing_context_crosses_to_the_older_segment() -> None:
    rows = [*standing_head(), row("4.0"), row("5.0")]
    page = window_or_cross(rows, "4.0", 2, older_available=True)
    assert (picked(rows, page), page.has_more, page.crossed) == (
        ["0.0", "1.0", "2.0", "3.0"],
        True,
        True,
    )
    last = window_or_cross(rows, "4.0", 2, older_available=False)
    assert (last.has_more, last.crossed) == (False, True)


def test_a_cursor_that_is_not_in_the_segment_yields_nothing() -> None:
    page = window_or_cross([row("1.0")], "7.0", 5, older_available=True)
    assert (page.indices, page.has_more, page.crossed) == ((), False, False)


def test_untagged_markers_are_not_standing_notes() -> None:
    rows = [row("0.0", "system_prompt"), row("1.0", "system_marker"), row("2.0")]
    page = window_or_cross(rows, "2.0", 5, older_available=True)
    assert page.crossed is False
    assert picked(rows, page) == ["0.0", "1.0"]


def test_the_initial_window_keeps_prompt_notes_and_summaries_ahead_of_the_tail() -> None:
    rows = [*standing_head(), *(row(f"{n}.0") for n in range(4, 12))]
    window = initial_window(
        rows, tail_size=3, tail_has_more=True, limit=3, historical_segments_available=False
    )
    assert picked(rows, window) == ["0.0", "1.0", "2.0", "3.0", "9.0", "10.0", "11.0"]
    assert window.has_more is True


def test_the_initial_window_reports_no_more_when_only_standing_context_is_missing() -> None:
    rows = [*standing_head(), row("4.0"), row("5.0")]
    window = initial_window(
        rows, tail_size=2, tail_has_more=True, limit=2, historical_segments_available=False
    )
    assert picked(rows, window) == ["0.0", "1.0", "2.0", "3.0", "4.0", "5.0"]
    assert window.has_more is False


def test_a_window_holding_the_prompt_attaches_nothing_and_keeps_history_visible() -> None:
    rows = [row("0.0", "system_prompt"), row("1.0")]
    window = initial_window(
        rows, tail_size=2, tail_has_more=False, limit=50, historical_segments_available=True
    )
    assert (picked(rows, window), window.has_more) == (["0.0", "1.0"], True)
