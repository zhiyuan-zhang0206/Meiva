# meiva:note
# The timeline's `before=` cursor: parsing a client-supplied item id and ordering item ids.
# meiva:link gateway/agents/history/docs/epoch-timeline.md

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

__all__ = [
    "MAX_CURSOR_LENGTH",
    "CursorPart",
    "TimelineCursor",
    "epoch_item_id",
    "item_sort_key",
    "parse_cursor",
]

MAX_CURSOR_LENGTH = 512
_MAX_CURSOR_INDEX_DIGITS = 20


SWITCH_BLOCK_ID = "sw"
UNAVAILABLE_ID = "unavailable"
_CURRENT_EPOCH_KEY = 2**63 - 1


class CursorPart(StrEnum):
    CONTENT = "content"
    SWITCH = "switch"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TimelineCursor:
    checkpoint_id: str | None
    msg_idx: int
    block_idx: int
    is_harness: bool = False
    epoch: int | None = None
    part: CursorPart = CursorPart.CONTENT

    def item_id(self, segment_prefix: str = "") -> str:
        prefix = "h" if self.is_harness else ""
        local_id = f"{prefix}{self.msg_idx}.{self.block_idx}"
        return f"{segment_prefix}.{local_id}" if segment_prefix else local_id


def epoch_item_id(seq: int, local_id: str) -> str:
    return f"e{seq}.{local_id}"


# meiva:note
# Parse current (`m.b`, `hm.b`), historical (`sK.cpid.m.b`) or earlier-epoch (`e<seq>.` before any of those,
# `e<seq>.sw`, `e<seq>.unavailable`) item ids.
#
# Historical ranks are syntax only. The checkpoint id is resolved against
# the current boundary index and determines the canonical rank used in the
# response, so a compact that shifts ranks cannot redirect a stale cursor.
def parse_cursor(before: str) -> TimelineCursor | None:
    if len(before) > MAX_CURSOR_LENGTH:
        return None
    if not before.startswith("e"):
        return _parse_local(before)
    head, _, rest = before.partition(".")
    seq = _parse_index(head[1:])
    if seq is None or (head[1:].startswith("0") and seq != 0):
        return None
    if rest in (SWITCH_BLOCK_ID, UNAVAILABLE_ID):
        part = CursorPart.SWITCH if rest == SWITCH_BLOCK_ID else CursorPart.UNAVAILABLE
        return TimelineCursor(None, 0, 0, epoch=seq, part=part)
    local = _parse_local(rest)
    return None if local is None else replace(local, epoch=seq)


def _parse_local(before: str) -> TimelineCursor | None:
    parts = before.split(".")
    if len(parts) == 2:
        msg, block = parts
        is_harness = msg.startswith("h")
        raw_msg = msg[1:] if is_harness else msg
        msg_idx = _parse_index(raw_msg)
        block_idx = _parse_index(block)
        if msg_idx is None or block_idx is None:
            return None
        return TimelineCursor(
            checkpoint_id=None, msg_idx=msg_idx, block_idx=block_idx, is_harness=is_harness
        )
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
    return TimelineCursor(
        checkpoint_id=checkpoint_id, msg_idx=msg_idx, block_idx=block_idx, is_harness=False
    )


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
# Order items by their position in the whole conversation, as a tuple: the epoch (an unprefixed id belongs to the
# current epoch, which sorts after every earlier one), the part within the epoch (the switch block, then the unavailable
# marker, then source items), the compact segment (older segments have the higher rank and come first) and the
# logical append position (msg_idx, block_idx) parsed from the local id, numerically, so "10.0" follows "2.0". This
# is the order the readers emit items in; sorting by created_at would reorder on a clock skew.
def item_sort_key(item_id: str) -> tuple[int, int, int, int, int]:
    epoch_key = _CURRENT_EPOCH_KEY
    local = item_id
    if item_id.startswith("e"):
        head, _, local = item_id.partition(".")
        epoch_key = int(head[1:])
    if local == SWITCH_BLOCK_ID:
        return (epoch_key, 0, 0, 0, 0)
    if local == UNAVAILABLE_ID:
        return (epoch_key, 1, 0, 0, 0)
    segments = local.split(".")
    rank = int(segments[0][1:]) if len(segments) == 4 else 0
    raw_msg = segments[-2].removeprefix("h")
    return (epoch_key, 2, -rank, int(raw_msg), int(segments[-1]))
