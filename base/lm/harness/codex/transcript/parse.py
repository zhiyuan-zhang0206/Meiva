from __future__ import annotations

from collections.abc import Collection

from base.lm.harness.codex.transcript.clock import iso_at
from base.lm.harness.codex.transcript.errors import MissingFieldError, UnknownValueError
from base.lm.harness.codex.transcript.items import (
    Where,
    as_anomaly,
    interrupt_entry,
    item_entries,
    remember_inputs,
    turn_end_entry,
)
from base.lm.harness.codex.transcript.rollout import parse_rollout, parse_rollout_line
from base.lm.harness.codex.transcript.scalars import opt_int, req_int, text
from base.lm.harness.codex.transcript.shapes import Obj, obj, objects, opt_objects
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    AnyEntry,
    Failure,
    Interrupt,
    Order,
    TurnEnd,
)

__all__ = [
    "parse_items_page",
    "parse_rollout",
    "parse_rollout_line",
    "parse_thread_read",
    "parse_turns",
]

# meiva:note
# Codex's own conversation record read into entries (`session/transcript/entries.py`). The primary
# input is the protocol history: a `thread/items/list` page (`parse_items_page`) or the turns of
# `thread/read` / `thread/turns/list` (`parse_turns`, `parse_thread_read`); the rollout
# (`parse_rollout`, `rollout.py`) supplements it with token usage, the synthetic `turn_aborted` and the
# sub-agent notification. `Order.seq` is the item's position in the page or thread parsed (the
# rollout ordinal for a rollout line) and `Order.sub` numbers the entries one item yields; a command
# that completes late arrives as its own call and result pair carrying its original turn id, and is
# appended when it arrives. A child thread is parsed with `record=<child thread id>`; the link from the
# parent's `spawnAgent` call names that id. Every function is pure and returns anomalies instead of
# raising: an item type the parser does not read is an `Anomaly(UNKNOWN_TYPE)` named by its type, a
# known one lacking a field the projection needs an `Anomaly(MISSING_FIELD)`; rows the parser knows to
# be bookkeeping give nothing.
#
# Item types not read yet (they report as UNKNOWN_TYPE): hookPrompt, functionCallOutput, plan,
# fileChange, mcpToolCall, dynamicToolCall, subAgentActivity, webSearch, imageView, sleep,
# imageGeneration, enteredReviewMode, exitedReviewMode. Rollout rows not read: `event_msg` kinds other
# than item_completed / task_complete / turn_aborted and the quiet ones in `rollout.py`, any
# `response_item` kind other than message / reasoning and the tool-call kinds, and the rollout items
# that mirror the unread types above. A turn's `error` is read for `failed` turns only; no recording
# of a failed turn, a server-started turn, an MCP call or a file change exists to check them against.
# meiva:link base/lm/harness/docs/transcript-projection.md


def parse_items_page(
    result: object, *, record: str = MAIN_RECORD, input_turns: Collection[str] = ()
) -> list[AnyEntry]:
    opened = set(input_turns)
    entries: list[AnyEntry] = []
    try:
        rows = objects(obj(result, "page"), "data")
    except MissingFieldError as error:
        return [as_anomaly(error, subject="page", order=Order(0, 0), record=record, turn=None)]
    for index, row in enumerate(rows):
        read = _row_entries(row, index, record, opened)
        remember_inputs(read, opened)
        entries.extend(read)
    return entries


def _row_entries(row: Obj, seq: int, record: str, opened: Collection[str]) -> list[AnyEntry]:
    try:
        item = obj(row.get("item"), "item")
        started = _time(row, item, "startedAtMs")
        where = Where(
            turn=text(row, "turnId"),
            record=record,
            started_ms=started,
            completed_ms=_time_or_none(row, item, "completedAtMs"),
            seq=seq,
        )
        return item_entries(item, where, opened)
    except MissingFieldError as error:
        order = Order(seq, 0)
        return [as_anomaly(error, subject=f"row{seq}", order=order, record=record, turn=None)]


def _time(row: Obj, item: Obj, field: str) -> int:
    value = _time_or_none(row, item, field)
    if value is None:
        raise MissingFieldError(field)
    return value


def _time_or_none(row: Obj, item: Obj, field: str) -> int | None:
    outer = opt_int(row, field)
    return outer if outer is not None else opt_int(item, field)


def parse_thread_read(result: object, *, record: str = MAIN_RECORD) -> list[AnyEntry]:
    try:
        turns = obj(obj(result, "result").get("thread"), "thread").get("turns")
    except MissingFieldError as error:
        return [as_anomaly(error, subject="thread", order=Order(0, 0), record=record, turn=None)]
    return parse_turns(turns, record=record)


# meiva:note
# The turns of a thread, each with its items and its `status`. The status gives the turn's boundary
# row: `completed` a `TurnEnd`, `interrupted` an `Interrupt`, `failed` a `Failure`; `inProgress` gives
# none. Position is the item's index in the thread (the boundary row follows its turn's items) and no
# time orders anything: the writer appends in arrival order. `at` is display only; a turn the server
# only inferred to be interrupted (a killed process) has no `completedAt` and so no `at`.
def parse_turns(turns: object, *, record: str = MAIN_RECORD) -> list[AnyEntry]:
    opened: set[str] = set()
    entries: list[AnyEntry] = []
    seq = 0
    try:
        listed = objects({"turns": turns}, "turns")
    except MissingFieldError as error:
        return [as_anomaly(error, subject="turns", order=Order(0, 0), record=record, turn=None)]
    for turn in listed:
        items = opt_objects(turn, "items")
        read, seq = _turn_entries(turn, items, seq, record, opened)
        entries.extend(read)
    return entries


def _turn_entries(
    turn: Obj, items: list[Obj], seq: int, record: str, opened: set[str]
) -> tuple[list[AnyEntry], int]:
    try:
        turn_id = text(turn, "id")
    except MissingFieldError as error:
        order = Order(seq, 0)
        return [
            as_anomaly(error, subject=f"turn{seq}", order=order, record=record, turn=None)
        ], seq + 1
    entries: list[AnyEntry] = []
    for item in items:
        read = _item_in_turn(item, turn_id, seq, record, opened)
        remember_inputs(read, opened)
        entries.extend(read)
        seq += 1
    entries.extend(_boundary(turn, turn_id, seq, record))
    return entries, seq + 1


def _item_in_turn(
    item: Obj, turn_id: str, seq: int, record: str, opened: Collection[str]
) -> list[AnyEntry]:
    try:
        started = req_int(item, "startedAtMs")
    except MissingFieldError as error:
        order = Order(seq, 0)
        return [as_anomaly(error, subject=f"item{seq}", order=order, record=record, turn=turn_id)]
    where = Where(turn_id, record, started, opt_int(item, "completedAtMs"), seq)
    return item_entries(item, where, opened)


def _boundary(turn: Obj, turn_id: str, seq: int, record: str) -> list[AnyEntry]:
    try:
        status = text(turn, "status")
        if status == "inProgress":
            return []
        ended = opt_int(turn, "completedAt")
        at_ms = None if ended is None else ended * 1000
        return [_closing_entry(status, turn, turn_id, at_ms, seq, record)]
    except (MissingFieldError, UnknownValueError) as error:
        order = Order(seq, 0)
        return [as_anomaly(error, subject=turn_id, order=order, record=record, turn=turn_id)]


def _closing_entry(
    status: str, turn: Obj, turn_id: str, at_ms: int | None, seq: int, record: str
) -> TurnEnd | Interrupt | Failure:
    match status:
        case "completed":
            return turn_end_entry(turn_id, at_ms, seq, record)
        case "interrupted":
            return interrupt_entry(turn_id, at_ms, seq, record)
        case "failed":
            return _failure(turn, turn_id, at_ms, seq, record)
        case _:
            raise UnknownValueError(f"turn.status={status}")


def _failure(turn: Obj, turn_id: str, at_ms: int | None, seq: int, record: str) -> Failure:
    return Failure(
        key=f"failure:{turn_id}",
        order=Order(seq, 0),
        record=record,
        turn=turn_id,
        at=None if at_ms is None else iso_at(at_ms),
        message=text(obj(turn.get("error"), "error"), "message"),
    )
