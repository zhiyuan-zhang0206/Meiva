from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.lm.harness.antigravity.transcript.errors import MissingError, UnrecognisedError
from base.lm.harness.antigravity.transcript.fields import (
    object_dict,
    optional_int,
    optional_list,
    optional_str,
    require_step,
    require_str,
)
from base.lm.harness.antigravity.transcript.system_message import classify
from base.lm.harness.antigravity.transcript.text import (
    background_notice,
    iso_of,
    user_text,
    valid_stamp,
    without_stamps,
)
from base.lm.harness.events import Usage
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    AgentText,
    Anomaly,
    AnomalyKind,
    AnyEntry,
    Compaction,
    HumanInput,
    Interrupt,
    Notice,
    Order,
    Reasoning,
    SubagentLink,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolStatus,
)

# meiva:note
# The Antigravity parser: raw `transcript_full.jsonl` rows in, entries out. It is pure. What it needs
# beyond one row is carried in an explicit `ParseState` that the caller keeps next to the record's
# position and hands back, so a reader that tails the file and one that re-reads it from a crash see the
# same entries with the same keys. Keys are `agy:<conversation id>:<step_index>[:<part>]`.
#
# Order. `seq` is `step_index`, so holes (a cancelled step leaves one) are allowed, and `sub` numbers the
# entries one row yields in the order they are shown (thinking, text, each call, then the usage; an
# output row's result, then its sub-agent links). No wall-clock time orders anything: `created_at` has
# second precision and is the step's creation time, so it is only carried as `Entry.at` for display, and
# rows are committed in `step_index` order, which is the order they are handed over.
#
# Provenance. The golden fixtures are CONSTRUCTED: the recordings kept no raw `transcript_full.jsonl`
# lines, so the rows were reconstructed from trajectory captures. A re-record of raw
# `transcript_full.jsonl` is required before the Antigravity cutover; until then the field shapes of
# `content` for system messages, tool outputs and checkpoints are unverified against the real file.
#
# Pairing. The record keeps no tool call id. Call `k` of the `PLANNER_RESPONSE` row at step `p` is
# given the id `agy:<conversation id>:<p>:<k>`, and its output is the `GENERIC` row at step `p + 1 + k`.
# Calls of the latest planner row that no row answered are `unanswered`; the record never says why (a
# cancel leaves a hole and no marker), so `cancel_entries` is how the cancel signal, which only the
# transport sees, ends them.
#
# Not covered yet, each reported (never guessed) when met: any row `type` other than USER_INPUT,
# PLANNER_RESPONSE, GENERIC, SYSTEM_MESSAGE and CHECKPOINT (`ERROR_MESSAGE` and the other 50 step types
# the binary names were never observed in a record); a row `status` other than DONE (GENERIC also
# RUNNING and ERROR; ERROR is documented in the CLI's prompts and was never observed); a non-zero exit
# code, which arrives as plain output and stays `ToolStatus.OK`; several `<USER_REQUEST>` blocks in one
# row and a prompt with several content blocks; `media` entries (the image reaches the timeline through
# the `SentInput` part, and `HumanInput` has no field for it); the chunked `chunks/` copies; compaction
# summaries beyond their text; the descriptor files under `subagents/`, whose `spawnStepIndex` is the
# last call of a parallel batch, so the link is read from the call's own output row instead; the real
# result of a background command, which exists only in the later notice and is not tied to its call.
# meiva:link base/lm/harness/docs/subagent-text.md

_CHILD_ID = re.compile(r'"conversationId"\s*:\s*"([^"]+)"')
_INVOKE_SUBAGENT = "invoke_subagent"


@dataclass(frozen=True, slots=True)
class OpenCall:
    step: int
    call_id: str
    name: str
    label: str | None


@dataclass(frozen=True, slots=True)
class ParseState:
    last_step: int = 0
    last_sub: int = 0
    ordinal: int = 0
    pending: tuple[OpenCall, ...] = ()
    unanswered: tuple[OpenCall, ...] = ()

    @property
    def open_calls(self) -> tuple[OpenCall, ...]:
        return self.unanswered + self.pending


# meiva:note
# What only the trajectory holds about a step: the `clientMessageId` we gave an input injected through
# the RPC (an ordinary prompt carries none) and whether the input joined a running turn
# (`isInterruptingStep`). Both are optional; without them an input has no `wire_id` and the projection
# recognises it by its text.
@dataclass(frozen=True, slots=True)
class TrajectorySide:
    client_message_ids: Mapping[int, str] = field(default_factory=dict[int, str])
    interrupting_steps: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class ParseResult:
    entries: tuple[AnyEntry, ...]
    state: ParseState


@dataclass(frozen=True, slots=True)
class _Row:
    conversation_id: str
    record: str
    step: int
    kind: str
    status: str
    at: str
    raw: Mapping[str, object]

    def key(self, part: str = "") -> str:
        base = f"agy:{self.conversation_id}:{self.step}"
        return f"{base}:{part}" if part else base

    def order(self, sub: int = 0) -> Order:
        return Order(self.step, sub)


_Handler = Callable[[_Row, ParseState, TrajectorySide], tuple[list[AnyEntry], ParseState]]


def parse_rows(
    rows: Iterable[object],
    *,
    conversation_id: str,
    record: str = MAIN_RECORD,
    state: ParseState | None = None,
    side: TrajectorySide | None = None,
) -> ParseResult:
    current = state if state is not None else ParseState()
    extra = side if side is not None else TrajectorySide()
    entries: list[AnyEntry] = []
    for raw in rows:
        produced, current = _parse_one(raw, conversation_id, record, extra, current)
        entries.extend(produced)
    return ParseResult(entries=tuple(entries), state=current)


def _parse_one(
    raw: object, conversation_id: str, record: str, side: TrajectorySide, state: ParseState
) -> tuple[list[AnyEntry], ParseState]:
    state = replace(state, ordinal=state.ordinal + 1)
    try:
        row = _header(raw, conversation_id, record)
    except MissingError as gap:
        key = f"agy:{conversation_id}:row:{state.ordinal}"
        order = Order(state.last_step, state.last_sub + 1)
        return [_anomaly(key, order, record, AnomalyKind.MISSING_FIELD, gap.detail)], state
    state = replace(state, last_step=row.step, last_sub=0)
    handler = _HANDLERS.get(row.kind)
    if handler is None:
        return [_row_anomaly(row, AnomalyKind.UNKNOWN_TYPE, row.kind)], state
    try:
        entries, state = handler(row, state, side)
        return entries, replace(state, last_sub=max((e.order.sub for e in entries), default=0))
    except MissingError as gap:
        return [_row_anomaly(row, AnomalyKind.MISSING_FIELD, f"{row.kind}.{gap.detail}")], state
    except UnrecognisedError as gap:
        return [_row_anomaly(row, AnomalyKind.UNKNOWN_TYPE, gap.detail)], state


def _header(raw: object, conversation_id: str, record: str) -> _Row:
    if not is_object_dict(raw):
        raise MissingError("row")
    created_at = require_str(raw, "created_at")
    return _Row(
        conversation_id=conversation_id,
        record=record,
        step=require_step(raw),
        kind=require_str(raw, "type"),
        status=require_str(raw, "status"),
        at=valid_stamp(created_at),
        raw=raw,
    )


def _anomaly(key: str, order: Order, record: str, anomaly: AnomalyKind, detail: str) -> Anomaly:
    return Anomaly(key=key, order=order, record=record, anomaly=anomaly, detail=detail)


def _row_anomaly(row: _Row, anomaly: AnomalyKind, detail: str) -> Anomaly:
    return _anomaly(row.key("anomaly"), row.order(), row.record, anomaly, detail)


def _user_input(
    row: _Row, state: ParseState, side: TrajectorySide
) -> tuple[list[AnyEntry], ParseState]:
    text = user_text(require_str(row.raw, "content"))
    entry = HumanInput(
        key=row.key(),
        order=row.order(),
        record=row.record,
        at=row.at,
        wire_id=side.client_message_ids.get(row.step),
        text=text,
        joined_running_turn=row.step in side.interrupting_steps,
    )
    return [entry], state


def _planner(
    row: _Row, state: ParseState, side: TrajectorySide
) -> tuple[list[AnyEntry], ParseState]:
    del side
    content = optional_str(row.raw, "content")
    thinking = optional_str(row.raw, "thinking")
    calls = _calls(row)
    entries: list[AnyEntry] = []
    if thinking and thinking.strip():
        entries.append(
            Reasoning(
                key=row.key("thinking"),
                order=row.order(0),
                record=row.record,
                at=row.at,
                text=thinking.strip(),
            )
        )
    if content and content.strip():
        entries.append(
            AgentText(
                key=row.key(),
                order=row.order(1),
                record=row.record,
                at=row.at,
                text=content.strip(),
                message_ref=str(row.step),
            )
        )
    entries.extend(_call_entry(row, call) for call in calls)
    entries.extend(_usage(row, 2 + len(calls)))
    opened = tuple(
        OpenCall(row.step + 1 + k, _call_id(row, k), name, _label(args)) for k, name, args in calls
    )
    return entries, replace(state, pending=opened, unanswered=state.unanswered + state.pending)


def _call_id(row: _Row, k: int) -> str:
    return f"agy:{row.conversation_id}:{row.step}:{k}"


def _calls(row: _Row) -> list[tuple[int, str, Mapping[str, object]]]:
    calls: list[tuple[int, str, Mapping[str, object]]] = []
    for k, item in enumerate(optional_list(row.raw, "tool_calls")):
        if not is_object_dict(item):
            raise MissingError(f"tool_calls[{k}]")
        args = item.get("args")
        if args is not None and not is_object_dict(args):
            raise MissingError(f"tool_calls[{k}].args")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise MissingError(f"tool_calls[{k}].name")
        calls.append((k, name, object_dict(args)))
    return calls


def _call_entry(row: _Row, call: tuple[int, str, Mapping[str, object]]) -> ToolCall:
    k, name, args = call
    return ToolCall(
        key=row.key(f"call:{k}"),
        order=row.order(2 + k),
        record=row.record,
        at=row.at,
        call_id=_call_id(row, k),
        name=name,
        tool_input=args,
    )


def _label(args: Mapping[str, object]) -> str | None:
    for name in ("Role", "TypeName"):
        value = args.get(name)
        if isinstance(value, str) and value:
            return value
    return None


# meiva:note
# `input_tokens` is the whole prompt and `cache_read_tokens` a part of it, as in the adapter's
# `parse_usage`; a figure that is the other way round (`cache_read_tokens` larger than the prompt) can
# only mean the cache is counted beside it, and is added. `output_tokens` already contains the thinking
# tokens. A row that reports no token field has no usage entry (unknown, not zero).
# meiva:link base/lm/harness/antigravity/mapping.py
def _usage(row: _Row, sub: int) -> list[AnyEntry]:
    prompt = optional_int(row.raw, "input_tokens")
    cached = optional_int(row.raw, "cache_read_tokens")
    output = optional_int(row.raw, "output_tokens")
    if prompt is None and output is None:
        return []
    raw_prompt, raw_cached = prompt or 0, cached or 0
    whole = raw_prompt + raw_cached if raw_cached > raw_prompt else raw_prompt
    usage = Usage(input_tokens=whole, output_tokens=output or 0, cache_read_input_tokens=raw_cached)
    return [
        TokenUsage(
            key=row.key("usage"),
            order=row.order(sub),
            record=row.record,
            at=row.at,
            message_ref=str(row.step),
            usage=usage,
        )
    ]


def _generic(
    row: _Row, state: ParseState, side: TrajectorySide
) -> tuple[list[AnyEntry], ParseState]:
    del side
    content = require_str(row.raw, "content")
    status = _tool_status(row.status)
    call = next((open_call for open_call in state.pending if open_call.step == row.step), None)
    if call is None:
        raise MissingError("tool_call")
    shown = (
        background_notice(content) if status is ToolStatus.BACKGROUND else without_stamps(content)
    )
    entries: list[AnyEntry] = [
        ToolResult(
            key=row.key(),
            order=row.order(),
            record=row.record,
            at=row.at,
            call_id=call.call_id,
            content=shown,
            status=status,
        )
    ]
    if call.name == _INVOKE_SUBAGENT:
        entries.extend(_links(row, call, content))
    remaining = tuple(open_call for open_call in state.pending if open_call is not call)
    return entries, replace(state, pending=remaining)


def _tool_status(status: str) -> ToolStatus:
    match status:
        case "DONE":
            return ToolStatus.OK
        case "RUNNING":
            return ToolStatus.BACKGROUND
        case "ERROR":
            return ToolStatus.ERROR
        case _:
            raise UnrecognisedError(f"status:{status}")


# meiva:note
# The output row of an `invoke_subagent` call names the child conversation(s) it created; that, and not
# the descriptor file, is what ties a child record to the call.
def _links(row: _Row, call: OpenCall, content: str) -> list[AnyEntry]:
    found = object_of(_CHILD_ID.findall, content)
    children = [c for c in found if isinstance(c, str)] if is_object_list(found) else []
    if not children:
        raise MissingError("conversationId")
    return [
        SubagentLink(
            key=row.key(f"link:{index}"),
            order=row.order(1 + index),
            record=row.record,
            at=row.at,
            call_id=call.call_id,
            child_record=child,
            label=call.label,
        )
        for index, child in enumerate(children)
    ]


def _system_message(
    row: _Row, state: ParseState, side: TrajectorySide
) -> tuple[list[AnyEntry], ParseState]:
    del side
    notice = classify(require_str(row.raw, "content"))
    if notice is None:
        return [], state
    entry = Notice(
        key=row.key(),
        order=row.order(),
        record=row.record,
        at=row.at,
        text=notice.text,
        cause=notice.cause,
    )
    return [entry], state


def _checkpoint(
    row: _Row, state: ParseState, side: TrajectorySide
) -> tuple[list[AnyEntry], ParseState]:
    del side
    summary = require_str(row.raw, "content")
    entries: list[AnyEntry] = [
        Compaction(key=row.key(), order=row.order(), record=row.record, at=row.at, summary=summary)
    ]
    entries.extend(_usage(row, 1))
    return entries, state


_HANDLERS: Mapping[str, _Handler] = {
    "USER_INPUT": _user_input,
    "PLANNER_RESPONSE": _planner,
    "GENERIC": _generic,
    "SYSTEM_MESSAGE": _system_message,
    "CHECKPOINT": _checkpoint,
}


# meiva:note
# A cancel is a signal (ACP `stopReason: cancelled`, stdout `result.error: interrupted`) and leaves no
# trace in the record except a hole where the cancelled step would be. The transport that saw the
# signal calls this with the record's state and the signal's time (shown only): it yields an `Interrupt`
# and an `ABORTED` result for every call no row answered, and a state with none open. Their order is the
# last step the parser read with the next `sub` values, so they follow the last parsed row and precede
# the next one. Calls already answered by a `RUNNING` row (background tasks) are not touched: the CLI
# keeps those running through a cancel.
def cancel_entries(
    state: ParseState, *, conversation_id: str, clock_ms: int, record: str = MAIN_RECORD
) -> ParseResult:
    prefix = f"agy:{conversation_id}:cancel:{state.last_step}"
    at = iso_of(clock_ms)
    first = state.last_sub + 1
    entries: list[AnyEntry] = [
        Interrupt(key=prefix, order=Order(state.last_step, first), record=record, at=at)
    ]
    entries.extend(
        ToolResult(
            key=f"{prefix}:{call.step}",
            order=Order(state.last_step, first + 1 + index),
            record=record,
            at=at,
            call_id=call.call_id,
            content="",
            status=ToolStatus.ABORTED,
        )
        for index, call in enumerate(state.open_calls)
    )
    last_sub = max(entry.order.sub for entry in entries)
    cleared = replace(state, pending=(), unanswered=(), last_sub=last_sub)
    return ParseResult(entries=tuple(entries), state=cleared)
