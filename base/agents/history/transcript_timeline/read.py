from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy
from base.agents.history.transcript_timeline.project import (
    Projected,
    ProjectedItem,
    RecordProjector,
)
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.claude.transcript.window import (
    BLOCK_BYTES,
    MAX_BLOCK_BYTES,
    EntryParser,
    FileRow,
    RecordFile,
    wire_id,
)

# meiva:note
# One window of a Claude session's timeline, read from the end of the record file: blocks are read backwards from
# the window's end until the rows hold `limit` items, so the bytes a request reads follow the size of the window
# and not of the file. The newest window ends at the file's last complete row; a window `before` an item ends where
# that item's row begins. Two things may need more than the window's own rows, and each is looked for further back
# by at most `MAX_LOOKBACK_BYTES` and then given up on: the result of a call whose call is not in the rows (it is
# read together with its call, so a pair is not split across two windows) and the start of the first item's turn
# (whether it was a person's or a background wake decides how its reply is shown). Past the limit, a result stands
# alone and a turn of unknown start counts as a person's. The look-back doubles at each step, so the rows read and
# projected in all are at most twice the final range, and a request reads at most `MAX_REQUEST_BYTES` (16 MiB): past it
# the window is what was found, with `has_more` set, however few items that is. Nothing is stored: the window is a function of the file and
# of the inbox rows of the same time. `size` is the file's size, the exclusive upper bound of every item's offset.
# meiva:link base/lm/harness/docs/transcript-projection.md

MAX_LOOKBACK_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = MAX_BLOCK_BYTES


# meiva:note
# `turn_start` is where the turn of the newest item begins (the earliest item of that turn that was read), for a live
# read that has to start from the turn it joins.
@dataclass(frozen=True, slots=True)
class Window:
    items: tuple[ProjectedItem, ...]
    has_more: bool
    size: int
    turn_start: int | None

    @property
    def timeline_items(self) -> list[TimelineItem]:
        return [projected.item for projected in self.items]


LoadSent = Callable[[datetime, Mapping[str, datetime]], Mapping[str, Sequence[TimelineItem]]]


def read_window(
    file: RecordFile,
    *,
    limit: int,
    before: int | None,
    policy: ReplyPolicy,
    render_tool: Callable[[str, Mapping[str, object]], str],
    load_sent: LoadSent,
) -> Window:
    size = file.size()
    tail_end = file.complete_end()
    end = tail_end if before is None else min(before, tail_end)
    rows: list[FileRow] = []
    start, spent, read, want = end, 0, 0, BLOCK_BYTES
    while True:
        block = file.rows_before(start, min(want, MAX_REQUEST_BYTES - read))
        rows = [*block.rows, *rows]
        grown, start = start - block.start, block.start
        read, want = read + grown, want * 2
        projected = _items_of(
            rows,
            tail=before is None,
            policy=policy,
            render_tool=render_tool,
            load_sent=load_sent,
        )
        cut = _cut(projected, limit)
        full = len(projected.items) - cut >= limit
        spent += grown if full else 0
        if (
            start == 0
            or spent >= MAX_LOOKBACK_BYTES
            or (full and _whole(projected, cut))
            or read >= MAX_REQUEST_BYTES
            or grown == 0
        ):
            return Window(
                items=projected.items[cut:],
                has_more=start > 0 or cut > 0,
                size=size,
                turn_start=projected.turn_start,
            )


def _items_of(
    rows: Sequence[FileRow],
    *,
    tail: bool,
    policy: ReplyPolicy,
    render_tool: Callable[[str, Mapping[str, object]], str],
    load_sent: LoadSent,
) -> Projected:
    projector = RecordProjector(policy=policy, render_tool=render_tool, wire_id=wire_id)
    wanted = input_wire_ids(rows)
    if wanted:
        for key, items in load_sent(max(wanted.values()), wanted).items():
            projector.register(key, items)
    entries = EntryParser().entries(rows)
    return projector.feed(entries, tail=tail, release_held=True)


# meiva:note
# The ids the CLI kept for the person's inputs among the rows, each with the time its row was written: the ids to match
# to inbox rows. A user row names its own `uuid`, an absorbed message the `source_uuid` of its attachment.
def input_wire_ids(rows: Sequence[FileRow]) -> dict[str, datetime]:
    wanted: dict[str, datetime] = {}
    for row in rows:
        stamp = _stamp(row.row.get("timestamp"))
        wire = _wire_of(row.row)
        if stamp is not None and wire is not None and is_input_row(row.row):
            wanted[wire] = stamp
    return wanted


def _wire_of(row: Mapping[str, object]) -> str | None:
    attachment = row.get("attachment")
    wire = attachment.get("source_uuid") if is_object_dict(attachment) else row.get("uuid")
    return wire if isinstance(wire, str) else None


# meiva:note
# A row that can be a person's input (a user row that is not a tool result, or an absorbed message): the only rows the
# inbox is asked about, so a stretch of the record with none costs no query.
def is_input_row(row: Mapping[str, object]) -> bool:
    if row.get("type") == "attachment":
        attachment = row.get("attachment")
        return is_object_dict(attachment) and attachment.get("type") == "queued_command"
    if row.get("type") != "user":
        return False
    origin = row.get("origin")
    if is_object_dict(origin) and origin.get("kind") not in (None, "human"):
        return False
    message = row.get("message")
    content = message.get("content") if is_object_dict(message) else None
    return not (
        is_object_list(content)
        and any(is_object_dict(block) and block.get("type") == "tool_result" for block in content)
    )


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# meiva:note
# The index the window starts at: the newest `limit` items, moved back to the call of a result that would be cut
# from it.
def _cut(projected: Projected, limit: int) -> int:
    items = projected.items
    cut = max(len(items) - limit, 0)
    if cut >= len(items):
        return cut
    first = items[cut]
    if first.is_call or first.call_id is None:
        return cut
    calls = [
        i for i, item in enumerate(items[:cut]) if item.is_call and item.call_id == first.call_id
    ]
    return calls[-1] if calls else cut


# meiva:note
# Whether what the window needs from before it is in the rows: the call of its first item when that is a result,
# and the opening of its turn.
def _whole(projected: Projected, cut: int) -> bool:
    items = projected.items
    if cut >= len(items):
        return True
    first = items[cut]
    if not first.is_call and first.call_id is not None:
        return False
    return first.turn is None or first.turn in projected.opened_turns
