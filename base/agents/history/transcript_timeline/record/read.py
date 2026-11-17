from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy
from base.agents.history.transcript_timeline.project import (
    Projected,
    ProjectedItem,
    RecordProjector,
)
from base.agents.history.transcript_timeline.record.harness import (
    entry_reader,
    input_wire_ids,
    wire_id,
)
from base.agents.history.transcript_timeline.record.match import CLAIM_SLACK
from base.api_contracts.reading import ReadingSourceUnavailableError, ReadingUnavailableReason
from base.lm.harness.session.transcript.entries import AgentText, InputRow, Reasoning
from base.lm.harness.session.transcript.record_file import (
    BLOCK_BYTES,
    MAX_BLOCK_BYTES,
    Block,
    FileRow,
    RecordFile,
    RecordReadBudgetError,
    stamp_of,
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

MAX_LOOKBACK_BYTES = MAX_BLOCK_BYTES
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
    has_newer: bool = False

    @property
    def timeline_items(self) -> list[TimelineItem]:
        return [projected.item for projected in self.items]

    def with_call_context(self, selected: Sequence[TimelineItem]) -> list[TimelineItem]:
        if not selected:
            return []
        first = next(
            i for i, item in enumerate(self.items) if item.item.item_id == selected[0].item_id
        )
        last = (
            next(
                i for i, item in enumerate(self.items) if item.item.item_id == selected[-1].item_id
            )
            + 1
        )
        cut = _cut(Projected(self.items[:last], (), 0, frozenset(), self.turn_start), last - first)
        return [item.item for item in self.items[cut:last]]


LoadSent = Callable[[datetime, Mapping[str, InputRow]], Mapping[str, Sequence[TimelineItem]]]


# meiva:note
# An opt-in parked read shares the committed-row parser and projector. Forward
# rows and turn/call context stay inside the plain request or compressed held
# region; inability to prove the requested source is explicit unavailability.
# meiva:link base/lm/harness/docs/transcript-projection.md
def read_reading_window(
    file: RecordFile,
    *,
    harness: str,
    offset: int,
    policy: ReplyPolicy,
    render_tool: Callable[[str, Mapping[str, object]], str],
    load_sent: LoadSent,
) -> Window:
    try:
        file.prepare_reading(offset)
        row = file.row_at(offset)
        if row is None:
            raise ReadingSourceUnavailableError(
                ReadingUnavailableReason.IDENTITY, "The complete target row is unavailable"
            )
        terminal = file.complete_record_end()
        forward = _forward_reading_rows(file, row.end, terminal)
        rows = [row, *forward.rows]
        start, want = row.offset, BLOCK_BYTES
        while True:
            block = file.rows_before(start, want)
            rows = [*block.rows, *rows]
            grown, start = start - block.start, block.start
            projected = _items_of(
                rows,
                harness=harness,
                tail=forward.end == terminal,
                complete_only=True,
                policy=policy,
                render_tool=render_tool,
                load_sent=load_sent,
            )
            if (
                start == 0
                or not projected.items
                or (_whole(projected, 0) and _input_context_complete(rows, projected, 0, harness))
            ):
                return Window(
                    projected.items,
                    start > 0,
                    file.size(),
                    projected.turn_start,
                    forward.end < terminal,
                )
            if grown == 0:
                raise ReadingSourceUnavailableError(
                    ReadingUnavailableReason.BUDGET,
                    "The source context exceeds the retained window",
                )
            want = min(want * 2, MAX_REQUEST_BYTES)
    except RecordReadBudgetError as exc:
        raise ReadingSourceUnavailableError(ReadingUnavailableReason.BUDGET, str(exc)) from exc


def _forward_reading_rows(file: RecordFile, start: int, terminal: int) -> Block:
    want = BLOCK_BYTES
    while True:
        block = file.rows_forward(start, want)
        if block.rows or start >= terminal:
            return block
        if want >= MAX_REQUEST_BYTES:
            raise ReadingSourceUnavailableError(
                ReadingUnavailableReason.BUDGET, "The next row exceeds the source window"
            )
        want = min(want * 2, MAX_REQUEST_BYTES)


def read_window(
    file: RecordFile,
    *,
    harness: str,
    limit: int,
    before: int | None,
    policy: ReplyPolicy,
    render_tool: Callable[[str, Mapping[str, object]], str],
    load_sent: LoadSent,
    anchor: int | None = None,
) -> Window:
    rows, start, read, size = _prepare_window(file, before, anchor)
    # meiva:note
    # An inclusive source at the last complete row is still live, even with partial bytes after it.
    # The full-record boundary is independent of the older buffer retained by a compressed anchor read.
    at_tail = before is None and (
        anchor is None or (bool(rows) and rows[-1].end == file.complete_record_end())
    )
    spent, want = 0, BLOCK_BYTES
    while True:
        block = file.rows_before(start, min(want, MAX_REQUEST_BYTES - read))
        rows = [*block.rows, *rows]
        grown, start = start - block.start, block.start
        read, want = read + grown, want * 2
        projected = _items_of(
            rows,
            harness=harness,
            tail=at_tail,
            complete_only=anchor is not None,
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
            or (
                full
                and _whole(projected, cut)
                and _input_context_complete(rows, projected, cut, harness)
            )
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
    harness: str,
    tail: bool,
    complete_only: bool,
    policy: ReplyPolicy,
    render_tool: Callable[[str, Mapping[str, object]], str],
    load_sent: LoadSent,
) -> Projected:
    projector = RecordProjector(
        policy=policy, render_tool=render_tool, wire_id=lambda key: wire_id(harness, key)
    )
    wanted = input_wire_ids(harness, rows)
    if wanted:
        for key, items in load_sent(max(row.stamp for row in wanted.values()), wanted).items():
            projector.register(key, items)
    reader = entry_reader(harness)
    entries = reader.entries(rows)
    projected = projector.feed(entries, tail=tail and reader.pending_calls, release_held=True)
    if not complete_only:
        return projected
    incomplete = {
        entry.key for entry in entries if isinstance(entry, AgentText) and not entry.complete
    }
    return replace(
        projected, items=tuple(item for item in projected.items if item.key not in incomplete)
    )


# meiva:note
# The index the window starts at: the newest `limit` items, moved back to the call of a result that would be cut
# from it.
def _cut(projected: Projected, limit: int) -> int:
    items = projected.items
    cut = max(len(items) - limit, 0)
    if cut >= len(items):
        return cut
    while True:
        previous = cut
        while cut > 0 and items[cut - 1].offset == items[cut].offset:
            cut -= 1
        first = items[cut]
        if not first.is_call and first.call_id is not None:
            calls = [
                i
                for i, item in enumerate(items[:cut])
                if item.is_call and item.call_id == first.call_id
            ]
            if calls:
                cut = calls[-1]
        if cut == previous:
            return cut


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


# meiva:note
# Text-matched inputs need the earlier inputs in their claim window as context, so a repeated
# message cannot move to a later input merely because a page starts there. The existing lookback
# and request budgets still bound this context; the file start is definitive.
def _input_context_complete(
    rows: Sequence[FileRow], projected: Projected, cut: int, harness: str
) -> bool:
    wanted = input_wire_ids(harness, rows)
    if not wanted or all(row.text is None for row in wanted.values()):
        return True
    stamp = stamp_of(projected.items[cut].item.created_at)
    return stamp is not None and min(row.stamp for row in wanted.values()) <= stamp - CLAIM_SLACK


# meiva:note
# Native timing changes revise only reasoning items at their original row IDs. Filtering before
# projection keeps inputs, assistant text, tools, receipts and delivery outside the replay.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
def reasoning_refresh(
    file: RecordFile,
    *,
    harness: str,
    start: int,
    policy: ReplyPolicy,
    render_tool: Callable[[str, Mapping[str, object]], str],
) -> Projected:
    entries = entry_reader(harness).entries(file.rows_after(start).rows)
    projector = RecordProjector(
        policy=policy, render_tool=render_tool, wire_id=lambda key: wire_id(harness, key)
    )
    return projector.feed(
        (entry for entry in entries if isinstance(entry, Reasoning)), tail=False, release_held=True
    )


def _prepare_window(
    file: RecordFile,
    before: int | None,
    anchor: int | None,
) -> tuple[list[FileRow], int, int, int]:
    if before is not None and anchor is not None:
        raise ValueError("A record window cannot use before and anchor together")
    if anchor is None:
        file.prepare(before)
        size = file.size()
        tail_end = file.complete_end()
        end = tail_end if before is None else min(before, tail_end)
        rows: list[FileRow] = []
        start, read = end, 0
    else:
        file.prepare_anchor(anchor)
        size = file.size()
        row = file.row_at(anchor)
        if row is None:
            return [], 0, 0, size
        rows = [row]
        start, read = row.offset, row.end - row.offset
    return rows, start, read, size
