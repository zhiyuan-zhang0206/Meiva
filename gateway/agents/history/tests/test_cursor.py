# meiva:note
# Parsing the timeline's `before=` cursor.

import pytest

from gateway.message_admission.cursor import (
    MAX_CURSOR_LENGTH,
    CursorPart,
    TimelineCursor,
    epoch_item_id,
    item_sort_key,
    parse_cursor,
)


def test_a_current_cursor_is_a_message_and_block_position() -> None:
    assert parse_cursor("12.3") == TimelineCursor(checkpoint_id=None, msg_idx=12, block_idx=3)


def test_a_historical_cursor_names_its_boundary_checkpoint() -> None:
    cursor = parse_cursor("s2.1f0b9b12-6000.4.0")
    assert cursor == TimelineCursor(checkpoint_id="1f0b9b12-6000", msg_idx=4, block_idx=0)
    assert cursor is not None
    assert cursor.item_id("s2.1f0b9b12-6000") == "s2.1f0b9b12-6000.4.0"
    assert cursor.item_id() == "4.0"


@pytest.mark.parametrize(
    "before",
    [
        "",
        "7",
        "a.b",
        "-1.0",
        "1.0.0",
        "s0.cp.1.0",
        "s01.cp.1.0",
        "x2.cp.1.0",
        "s2..1.0",
        "s2.cp.1.x",
        f"{'1' * 5000}.0",
        f"s1.boundary.1.{'0' * 5000}",
        f"{'9' * 21}.0",
        "١.0",
    ],
)
def test_anything_else_is_not_a_cursor(before: str) -> None:
    assert parse_cursor(before) is None


def test_a_cursor_past_the_length_bound_is_not_a_cursor() -> None:
    assert parse_cursor("1." + "0" * MAX_CURSOR_LENGTH) is None


def test_item_ids_order_numerically_not_lexically() -> None:
    assert item_sort_key("2.0") < item_sort_key("10.0")
    assert item_sort_key("3.2") < item_sort_key("3.10")
    assert item_sort_key("h2.0") < item_sort_key("h10.0")
    assert item_sort_key("h3.2")[3:] == (3, 2)


def test_older_compact_segments_order_before_newer_ones() -> None:
    assert item_sort_key("s2.cp.9.0") < item_sort_key("s1.cp.1.0") < item_sort_key("0.0")


def test_an_earlier_epoch_orders_before_the_current_one_and_its_parts_in_order() -> None:
    in_order = [
        "e0.s1.cp.3.0",
        "e0.0.0",
        "e0.12.0",
        "e1.sw",
        "e1.h5.0",
        "e1.h9000.2",
        "e2.sw",
        "3.0",
    ]
    shuffled = [*in_order[4:], *in_order[:4]]
    assert sorted(shuffled, key=item_sort_key) == in_order


@pytest.mark.parametrize(
    ("before", "epoch", "part", "local_id"),
    [
        ("e0.12.0", 0, CursorPart.CONTENT, "12.0"),
        ("e3.h900.1", 3, CursorPart.CONTENT, "h900.1"),
        ("e1.s2.cp.4.0", 1, CursorPart.CONTENT, "4.0"),
        ("e2.sw", 2, CursorPart.SWITCH, "0.0"),
    ],
)
def test_an_epoch_cursor_names_its_epoch_and_part(
    before: str, epoch: int, part: CursorPart, local_id: str
) -> None:
    cursor = parse_cursor(before)
    assert cursor is not None
    assert (cursor.epoch, cursor.part, cursor.item_id()) == (epoch, part, local_id)


def test_an_epoch_item_id_is_the_local_id_behind_the_epoch_prefix() -> None:
    assert epoch_item_id(2, "h900.1") == "e2.h900.1"


@pytest.mark.parametrize(
    "before",
    ["e", "e.0.0", "e01.0.0", "ex.0.0", "e1", "e1.", "e1.x", "e1.e2.0.0", "e1.sw.0", "e-1.0.0"],
)
def test_a_malformed_epoch_cursor_is_not_a_cursor(before: str) -> None:
    assert parse_cursor(before) is None


def test_a_harness_cursor_parses_with_harness_flag() -> None:
    cursor = parse_cursor("h12.3")
    assert cursor == TimelineCursor(checkpoint_id=None, msg_idx=12, block_idx=3, is_harness=True)
    assert cursor is not None
    assert cursor.item_id() == "h12.3"
