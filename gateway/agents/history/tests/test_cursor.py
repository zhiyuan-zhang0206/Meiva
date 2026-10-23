# meiva:note
# Parsing the timeline's `before=` cursor.

import pytest

from gateway.agents.history.cursor import (
    MAX_CURSOR_LENGTH,
    TimelineCursor,
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
    assert item_sort_key("s1.cp.4.1") == (4, 1)
