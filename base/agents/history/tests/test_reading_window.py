from __future__ import annotations

import pytest

from base.agents.history.timeline import reading_target, reading_window
from base.agents.history.timeline_item import TimelineItem
from base.api_contracts.reading import (
    NativeReadingLocator,
    ReadingDirection,
    ReadingSourceUnavailableError,
    ReadingUnavailableReason,
    RecordReadingLocator,
    decode_reading_locator,
    encode_reading_locator,
)


def rows(count: int) -> list[TimelineItem]:
    return [
        TimelineItem(
            item_id=f"h{offset}.{block}",
            kind="agent_chat",
            payload=f"row {offset} block {block}",
            reading_locator=encode_reading_locator(
                RecordReadingLocator(
                    agent_id=42, harness="codex", session_id="session-1", offset=offset, block=block
                )
            ),
        )
        for offset in range(count)
        for block in range(2)
    ]


def test_anchor_contains_exact_block_and_keeps_its_row_whole() -> None:
    items = rows(3)
    result = reading_window(
        items, 3, ReadingDirection.ANCHOR, 1, older_available=False, newer_available=False
    )
    assert result.anchor_item_id == "h1.1"
    assert [item.item_id for item in result.items] == ["h1.0", "h1.1"]
    older = decode_reading_locator(result.older_cursor or "")
    newer = decode_reading_locator(result.newer_cursor or "")
    assert isinstance(older, RecordReadingLocator)
    assert isinstance(newer, RecordReadingLocator)
    assert older.item_id() == "h1.0"
    assert newer.item_id() == "h1.1"


@pytest.mark.parametrize(
    "direction,target,expected",
    [
        (ReadingDirection.BEFORE, 3, ["h0.0", "h0.1"]),
        (ReadingDirection.AFTER, 2, ["h2.0", "h2.1"]),
    ],
)
def test_directional_page_excludes_the_entire_input_row(
    direction: ReadingDirection, target: int, expected: list[str]
) -> None:
    items = rows(3)
    result = reading_window(
        items, target, direction, 1, older_available=False, newer_available=False
    )
    assert [item.item_id for item in result.items] == expected
    assert result.anchor_item_id is None
    for cursor in [result.older_cursor, result.newer_cursor]:
        assert cursor is None or cursor != items[target].reading_locator


@pytest.mark.parametrize(
    "direction,target", [(ReadingDirection.BEFORE, 0), (ReadingDirection.AFTER, 3)]
)
def test_proven_source_end_is_an_empty_terminal_page(
    direction: ReadingDirection, target: int
) -> None:
    result = reading_window(
        rows(2), target, direction, 10, older_available=False, newer_available=False
    )
    assert result.items == []
    assert result.older_cursor is None
    assert result.newer_cursor is None


def test_unloaded_next_segment_does_not_masquerade_as_proven_source_end() -> None:
    with pytest.raises(ReadingSourceUnavailableError) as exc:
        _ = reading_window(
            rows(2), 3, ReadingDirection.AFTER, 10, older_available=False, newer_available=True
        )
    assert exc.value.reason is ReadingUnavailableReason.SOURCE


def test_identityless_edge_does_not_fall_back_to_the_input_cursor() -> None:
    items = rows(3)
    items[2] = items[2].model_copy(update={"reading_locator": None})
    items[3] = items[3].model_copy(update={"reading_locator": None})
    with pytest.raises(ReadingSourceUnavailableError) as exc:
        _ = reading_window(
            items, 0, ReadingDirection.AFTER, 1, older_available=False, newer_available=False
        )
    assert exc.value.reason is ReadingUnavailableReason.IDENTITY


def test_forward_pages_make_row_progress_to_a_terminal_window() -> None:
    items = rows(5)
    target = 0
    seen: list[str] = []
    while True:
        result = reading_window(
            items, target, ReadingDirection.AFTER, 1, older_available=False, newer_available=False
        )
        seen.extend(item.item_id for item in result.items)
        if result.newer_cursor is None:
            break
        next_target = next(
            i for i, item in enumerate(items) if item.reading_locator == result.newer_cursor
        )
        assert next_target > target
        target = next_target
    assert seen == [item.item_id for item in items[2:]]
    assert len(set(seen)) == len(seen)


def test_native_identity_survives_a_changed_local_coordinate_without_guessing() -> None:
    locator = NativeReadingLocator(agent_id=42, epoch="initial", message_id="stable", block=1)
    same_index_other_message = TimelineItem(
        item_id="2.1",
        kind="agent_chat",
        payload="other",
        source_message_id="other",
        source_block_idx=1,
    )
    moved = same_index_other_message.model_copy(
        update={"item_id": "s1.boundary.9.1", "source_message_id": "stable"}
    )
    assert reading_target([same_index_other_message, moved], locator) == 1
    with pytest.raises(ReadingSourceUnavailableError) as exc:
        _ = reading_target([same_index_other_message], locator)
    assert exc.value.reason is ReadingUnavailableReason.IDENTITY


@pytest.mark.parametrize(
    "changes", [{"source_inbound_id": 12}, {"source_block_idx": 0}, {"source_message_id": "other"}]
)
def test_every_supplied_native_identity_and_block_must_match(changes: dict[str, object]) -> None:
    locator = NativeReadingLocator(
        agent_id=42, epoch="initial", message_id="stable", inbound_id=11, block=1
    )
    item = TimelineItem(
        item_id="2.1",
        kind="inbound_chat",
        payload="saved",
        source_message_id="stable",
        source_inbound_id=11,
        source_block_idx=1,
    )
    assert reading_target([item], locator) == 0
    with pytest.raises(ReadingSourceUnavailableError):
        _ = reading_target([item.model_copy(update=changes)], locator)


def test_duplicate_stable_identity_is_unavailable_instead_of_first_match() -> None:
    locator = NativeReadingLocator(agent_id=42, epoch="initial", inbound_id=11, block=0)
    item = TimelineItem(
        item_id="2.0",
        kind="inbound_chat",
        payload="saved",
        source_inbound_id=11,
        source_block_idx=0,
    )
    with pytest.raises(ReadingSourceUnavailableError):
        _ = reading_target([item, item.model_copy(update={"item_id": "3.0"})], locator)
