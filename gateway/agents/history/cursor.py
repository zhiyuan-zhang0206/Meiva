# meiva:note
# The timeline's `before=` cursor: parsing a client-supplied item id and ordering item ids.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["MAX_CURSOR_LENGTH", "TimelineCursor", "item_sort_key", "parse_cursor"]

MAX_CURSOR_LENGTH = 512
_MAX_CURSOR_INDEX_DIGITS = 20


@dataclass(frozen=True)
class TimelineCursor:
    checkpoint_id: str | None
    msg_idx: int
    block_idx: int

    def item_id(self, segment_prefix: str = "") -> str:
        local_id = f"{self.msg_idx}.{self.block_idx}"
        return f"{segment_prefix}.{local_id}" if segment_prefix else local_id


# meiva:note
# Parse current (`m.b`) or historical (`sK.cpid.m.b`) item ids.
#
# Historical ranks are syntax only. The checkpoint id is resolved against
# the current boundary index and determines the canonical rank used in the
# response, so a compact that shifts ranks cannot redirect a stale cursor.
def parse_cursor(before: str) -> TimelineCursor | None:
    if len(before) > MAX_CURSOR_LENGTH:
        return None
    parts = before.split(".")
    if len(parts) == 2:
        msg, block = parts
        msg_idx = _parse_index(msg)
        block_idx = _parse_index(block)
        if msg_idx is None or block_idx is None:
            return None
        return TimelineCursor(checkpoint_id=None, msg_idx=msg_idx, block_idx=block_idx)
    if len(parts) != 4:
        return None
    rank, checkpoint_id, msg, block = parts
    rank_value = rank[1:] if rank.startswith("s") else ""
    rank_idx = _parse_index(rank_value)
    msg_idx = _parse_index(msg)
    block_idx = _parse_index(block)
    if rank_idx is None or rank_idx == 0 or rank_value.startswith("0") or not checkpoint_id:
        return None
    if msg_idx is None or block_idx is None:
        return None
    return TimelineCursor(checkpoint_id=checkpoint_id, msg_idx=msg_idx, block_idx=block_idx)


# meiva:note
# Parse one bounded ASCII item-id integer without integer-limit errors.
def _parse_index(value: str) -> int | None:
    if (
        not value
        or len(value) > _MAX_CURSOR_INDEX_DIGITS
        or not value.isascii()
        or not value.isdigit()
    ):
        return None
    try:
        return int(value)
    except ValueError:
        return None


# meiva:note
# Order items by their logical append position (msg_idx, block_idx) parsed
# from item_id — numeric, so "10.0" follows "2.0". This is the order
# build_timeline_items already emits in; sorting by created_at would reorder
# on a clock skew now that items carry real wall-clock timestamps.
def item_sort_key(item_id: str) -> tuple[int, int]:
    *_, msg_idx, block_idx = item_id.split(".")
    return (int(msg_idx), int(block_idx))
