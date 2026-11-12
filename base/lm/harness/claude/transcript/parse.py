from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TypedDict

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.claude.delivery import harness_cause
from base.lm.harness.events import HarnessCause, Usage
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    AgentText,
    Anomaly,
    AnomalyKind,
    AnyEntry,
    Compaction,
    Failure,
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
    TurnEnd,
)

# meiva:note
# Claude Code's transcript, read into the shared entry vocabulary. One row in, entries out; the only state
# is `ParseState` (the start of each open tool call, for its duration), and it is returned, never mutated.
# `Order` is the row's own line, so it follows file order; the CLI's timestamps are not monotone in file order
# and are used for display and for a call's duration only. Observed on CLI 2.1.322; the format is the CLI's own and unpublished, so a
# row of a type this module does not know is an `Anomaly`, never a guess.
#
# Rows read: `user` (our inputs and the CLI's synthetic text, tool results), `assistant` (one content
# block per row, a message's blocks sharing `message.id`), `system/compact_boundary`, and the
# `attachment/queued_command` of an input absorbed mid-turn. Rows ignored on purpose: the bookkeeping types
# and context attachments listed below, the `isMeta` and compact-summary rows, the CLI's synthetic
# `<synthetic>` assistant row ("No response requested.") and a sub-agent's prompt echo. A synthetic assistant row
# with `isApiErrorMessage` is the CLI's record of an API error (`error`, `apiErrorStatus`): a `Failure`.
# Observed on a long real session and ignored on purpose too: title, link and rename rows, file-history deltas, the
# `informational` and `stop_hook_summary` system subtypes and the context attachments of the second list below.
# Not read yet: any other `system` subtype than `compact_boundary` and the attachment types of newer CLIs
# are reported as anomalies; hook output, API error rows and `isApiErrorMessage` rows have no sample.
# meiva:link base/lm/harness/docs/transcript-projection.md

_BOOKKEEPING = frozenset(
    {
        "queue-operation",
        "file-history-snapshot",
        "last-prompt",
        "cost-state",
        "session-owner",
        "tools-used",
        "mode",
        "atis-latch",
        "ai-title",
        "custom-title",
        "agent-name",
        "pr-link",
        "bridge-session",
        "relocated",
        "file-history-delta",
        "api-request",
        "api-request-shape",
        "api-request-blob",
    }
)
_CONTEXT_ATTACHMENTS = frozenset(
    {
        "hook_success",
        "hook_additional_context",
        "environment",
        "model",
        "language",
        "deferred_tools_delta",
        "agent_listing_delta",
        "skill_listing",
        "total_tokens_reminder",
        "session_context",
        "date",
        "credential_org",
        "remote_session_change",
        "prompt_snapshot",
        "mcp_instructions_delta",
        "deferred_tools_record",
        "instructions",
        "file",
        "silent_turn_reminder",
        "task_status",
        "edited_text_file",
        "thinking_drop",
        "compact_file_reference",
        "command_permissions",
        "nested_memory",
        "read_truncation_notice",
    }
)
_IGNORED_SYSTEM = frozenset({"informational", "stop_hook_summary"})
_ENDING_STOPS = frozenset({"end_turn", "max_tokens", "refusal", "stop_sequence"})
_INTERRUPT_PREFIX = "[Request interrupted by user"
_LOCAL_COMMAND_PREFIXES = ("<local-command-stdout>", "<local-command-caveat>")
_SYNTHETIC_MODEL = "<synthetic>"
_TAG = re.compile(r"<(task-id|tool-use-id|status|summary)>(.*?)</\1>", re.DOTALL)


@dataclass(frozen=True, slots=True)
class ParseState:
    call_ms: Mapping[str, int] = field(default_factory=dict[str, int])


@dataclass(frozen=True, slots=True)
class _Row:
    record: str
    uuid: str
    at: str
    ms: int
    line: int


def row_version(raw: Mapping[str, object]) -> str | None:
    version = raw.get("version")
    return version if isinstance(version, str) else None


def parse_row(
    state: ParseState, raw: Mapping[str, object], *, line: int
) -> tuple[ParseState, tuple[AnyEntry, ...]]:
    row_type = raw.get("type")
    if not isinstance(row_type, str):
        return state, (_anomaly(raw, line, AnomalyKind.MISSING_FIELD, "type"),)
    if row_type in _BOOKKEEPING:
        return state, ()
    if row_type == "attachment":
        return _attachment(state, raw, line)
    if row_type in ("user", "assistant", "system"):
        return _conversation(state, raw, row_type, line)
    return state, (_anomaly(raw, line, AnomalyKind.UNKNOWN_TYPE, row_type),)


def _conversation(
    state: ParseState, raw: Mapping[str, object], row_type: str, line: int
) -> tuple[ParseState, tuple[AnyEntry, ...]]:
    located = _locate(state, raw, line)
    if isinstance(located, Anomaly):
        return state, (located,)
    row, state = located
    if row_type == "system":
        return state, _system(row, raw)
    message = raw.get("message")
    if not is_object_dict(message):
        return state, (_missing(row, "message"),)
    if row_type == "assistant":
        return _assistant(state, row, raw, message)
    return _user(state, row, raw, message)


def _locate(
    state: ParseState, raw: Mapping[str, object], line: int
) -> tuple[_Row, ParseState] | Anomaly:
    uuid, at = raw.get("uuid"), raw.get("timestamp")
    record = MAIN_RECORD
    if raw.get("isSidechain") is True:
        agent = raw.get("agentId")
        if not isinstance(agent, str) or not agent:
            return _anomaly(raw, line, AnomalyKind.MISSING_FIELD, "agentId")
        record = agent
    if not isinstance(uuid, str) or not uuid:
        return _anomaly(raw, line, AnomalyKind.MISSING_FIELD, "uuid")
    ms = _millis(at)
    if not isinstance(at, str) or ms is None:
        return _anomaly(raw, line, AnomalyKind.MISSING_FIELD, "timestamp")
    return _Row(record, uuid, at, ms, line), state


def _millis(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return None


def _anomaly(raw: Mapping[str, object], line: int, kind: AnomalyKind, detail: str) -> Anomaly:
    uuid = raw.get("uuid")
    key = f"claude:anomaly:{uuid if isinstance(uuid, str) else line}:{kind.value}"
    return Anomaly(key=key, order=Order(line), anomaly=kind, detail=detail)


def _missing(row: _Row, detail: str) -> Anomaly:
    return Anomaly(
        key=f"claude:anomaly:{row.uuid}:{AnomalyKind.MISSING_FIELD.value}",
        order=Order(row.line),
        record=row.record,
        anomaly=AnomalyKind.MISSING_FIELD,
        detail=detail,
    )


class _Fields(TypedDict):
    key: str
    order: Order
    record: str
    at: str


def _entry_fields(row: _Row, sub: int = 0, *, key: str | None = None) -> _Fields:
    return {
        "key": key or f"claude:{row.uuid}",
        "order": Order(row.line, sub),
        "record": row.record,
        "at": row.at,
    }


def _system(row: _Row, raw: Mapping[str, object]) -> tuple[AnyEntry, ...]:
    subtype = raw.get("subtype")
    if subtype == "compact_boundary":
        return (Compaction(**_entry_fields(row)),)
    if subtype in _IGNORED_SYSTEM:
        return ()
    detail = f"system/{subtype}" if isinstance(subtype, str) else "system"
    return (
        Anomaly(
            key=f"claude:anomaly:{row.uuid}:{AnomalyKind.UNKNOWN_TYPE.value}",
            order=Order(row.line),
            record=row.record,
            anomaly=AnomalyKind.UNKNOWN_TYPE,
            detail=detail,
        ),
    )


def _attachment(
    state: ParseState, raw: Mapping[str, object], line: int
) -> tuple[ParseState, tuple[AnyEntry, ...]]:
    attachment = raw.get("attachment")
    kind = attachment.get("type") if is_object_dict(attachment) else None
    if not is_object_dict(attachment) or not isinstance(kind, str):
        return state, (_anomaly(raw, line, AnomalyKind.MISSING_FIELD, "attachment.type"),)
    if kind in _CONTEXT_ATTACHMENTS:
        return state, ()
    if kind != "queued_command":
        return state, (_anomaly(raw, line, AnomalyKind.UNKNOWN_TYPE, f"attachment/{kind}"),)
    located = _locate(state, raw, line)
    if isinstance(located, Anomaly):
        return state, (located,)
    row, state = located
    source, prompt = attachment.get("source_uuid"), attachment.get("prompt")
    if not isinstance(prompt, str):
        return state, (_missing(row, "attachment.prompt"),)
    origin = attachment.get("origin")
    origin_kind = origin.get("kind") if is_object_dict(origin) else None
    if isinstance(origin_kind, str):
        if harness_cause(origin_kind) is HarnessCause.UNKNOWN:
            return state, (_anomaly(raw, line, AnomalyKind.UNKNOWN_TYPE, f"origin/{origin_kind}"),)
        call_id, rendered = _notification(prompt)
        notice = Notice(
            **_entry_fields(row),
            text=rendered,
            cause=harness_cause(origin_kind),
            call_id=call_id,
            opens_turn=False,
        )
        return state, (notice,)
    if not isinstance(source, str):
        return state, (_missing(row, "attachment.source_uuid"),)
    steer = HumanInput(
        **_entry_fields(row),
        wire_id=source,
        text=prompt,
        joined_running_turn=True,
    )
    return state, (steer,)


def _assistant(
    state: ParseState, row: _Row, raw: Mapping[str, object], message: Mapping[str, object]
) -> tuple[ParseState, tuple[AnyEntry, ...]]:
    if raw.get("isApiErrorMessage") is True:
        return state, _api_error(row, raw, message)
    if message.get("model") == _SYNTHETIC_MODEL:
        return state, ()
    blocks = message.get("content")
    if not is_object_list(blocks):
        return state, (_missing(row, "message.content"),)
    ref = message.get("id")
    if not isinstance(ref, str):
        return state, (_missing(row, "message.id"),)
    aborted = raw.get("isAbortedMidStream") is True
    entries: list[AnyEntry] = []
    call_ms = dict(state.call_ms)
    for index, block in enumerate(blocks):
        key = f"claude:{row.uuid}" if len(blocks) == 1 else f"claude:{row.uuid}:{index}"
        entry = _block(row, key, index, block, raw, aborted=aborted)
        if isinstance(entry, ToolCall):
            call_ms[entry.call_id] = row.ms
        if entry is not None:
            entries.append(entry)
    entries.append(
        _usage_entry(row, ref, message.get("usage"), message.get("model"), reliable=not aborted)
    )
    stop = message.get("stop_reason")
    if row.record == MAIN_RECORD and isinstance(stop, str) and stop in _ENDING_STOPS:
        entries.append(TurnEnd(**_entry_fields(row, 2, key=f"claude:{row.uuid}:end")))
    return ParseState(call_ms), tuple(entries)


def _api_error(
    row: _Row, raw: Mapping[str, object], message: Mapping[str, object]
) -> tuple[AnyEntry, ...]:
    text = _text(message.get("content"))
    code = raw.get("error")
    detail = text or (code if isinstance(code, str) else "")
    if not detail:
        return (_missing(row, "message.content"),)
    return (Failure(**_entry_fields(row), message=detail),)


def _block(
    row: _Row,
    key: str,
    index: int,
    block: object,
    raw: Mapping[str, object],
    *,
    aborted: bool,
) -> AnyEntry | None:
    if not is_object_dict(block):
        return _missing(row, "message.content[]")
    fields = _entry_fields(row, index * 10, key=key)
    kind = block.get("type")
    if kind == "thinking":
        text = block.get("thinking")
        duration = raw.get("thinkingDurationMs")
        return Reasoning(
            **fields,
            text=text if isinstance(text, str) else "",
            duration_ms=duration if isinstance(duration, int) else None,
        )
    if kind == "text":
        text = block.get("text")
        if not isinstance(text, str):
            return _missing(row, "text")
        ref = _message_ref(raw)
        return AgentText(**fields, text=text, message_ref=ref, complete=not aborted)
    if kind == "tool_use":
        call_id, name, tool_input = block.get("id"), block.get("name"), block.get("input")
        if not isinstance(call_id, str) or not isinstance(name, str):
            return _missing(row, "tool_use.id")
        return ToolCall(
            **fields,
            call_id=call_id,
            name=name,
            tool_input=tool_input if is_object_dict(tool_input) else {},
        )
    return Anomaly(
        key=f"claude:anomaly:{row.uuid}:{index}:{AnomalyKind.UNKNOWN_TYPE.value}",
        order=Order(row.line, index * 10),
        record=row.record,
        anomaly=AnomalyKind.UNKNOWN_TYPE,
        detail=f"content/{kind}",
    )


def _message_ref(raw: Mapping[str, object]) -> str | None:
    message = raw.get("message")
    ref = message.get("id") if is_object_dict(message) else None
    return ref if isinstance(ref, str) else None


def _usage_entry(
    row: _Row, ref: str, raw_usage: object, model: object, *, reliable: bool
) -> TokenUsage:
    data = raw_usage if is_object_dict(raw_usage) else {}
    return TokenUsage(
        **_entry_fields(row, 1, key=f"claude:{row.uuid}:usage"),
        message_ref=ref,
        usage=_usage(data),
        reliable=reliable,
        model=model if isinstance(model, str) else None,
    )


# meiva:note
# Anthropic's `input_tokens` is the uncached remainder; the two cache counts are charged separately, so the
# whole prompt is their sum, which is the shape `Usage` carries everywhere else.
def _usage(data: Mapping[str, object]) -> Usage:
    def count(name: str) -> int:
        value = data.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    cache_read, cache_write = count("cache_read_input_tokens"), count("cache_creation_input_tokens")
    return Usage(
        input_tokens=count("input_tokens") + cache_read + cache_write,
        output_tokens=count("output_tokens"),
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cache_read,
    )


def _user(
    state: ParseState, row: _Row, raw: Mapping[str, object], message: Mapping[str, object]
) -> tuple[ParseState, tuple[AnyEntry, ...]]:
    if raw.get("isMeta") is True or raw.get("isCompactSummary") is True:
        return state, ()
    content = message.get("content")
    if is_object_list(content) and any(
        is_object_dict(block) and block.get("type") == "tool_result" for block in content
    ):
        return _results(state, row, raw, content)
    text = _text(content)
    if text is None:
        return state, (_missing(row, "message.content"),)
    return state, _user_text(row, raw, text)


def _text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if not is_object_list(content):
        return None
    parts = [
        block["text"]
        for block in content
        if is_object_dict(block)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n".join(str(part) for part in parts)


def _user_text(row: _Row, raw: Mapping[str, object], text: str) -> tuple[AnyEntry, ...]:
    if text.startswith(_INTERRUPT_PREFIX):
        return (Interrupt(**_entry_fields(row)),)
    if raw.get("isSidechain") is True or text.startswith(_LOCAL_COMMAND_PREFIXES):
        return ()
    turn = raw.get("promptId")
    origin = raw.get("origin")
    kind = origin.get("kind") if is_object_dict(origin) else None
    fields = _entry_fields(row)
    turn_name = turn if isinstance(turn, str) else None
    if isinstance(kind, str):
        call_id, rendered = _notification(text)
        return (
            Notice(
                **fields,
                turn=turn_name,
                text=rendered,
                cause=harness_cause(kind),
                call_id=call_id,
            ),
        )
    return (HumanInput(**fields, turn=turn_name, wire_id=row.uuid, text=text),)


def _notification(text: str) -> tuple[str | None, str]:
    tags = {match.group(1): match.group(2).strip() for match in _TAG.finditer(text)}
    task, status = tags.get("task-id"), tags.get("status")
    if task is None or status is None:
        return None, text
    summary = tags.get("summary")
    rendered = f"Background task {task} {status}" + (f": {summary}" if summary else ".")
    return tags.get("tool-use-id"), rendered


def _results(
    state: ParseState, row: _Row, raw: Mapping[str, object], blocks: list[object]
) -> tuple[ParseState, tuple[AnyEntry, ...]]:
    entries: list[AnyEntry] = []
    call_ms = dict(state.call_ms)
    result = raw.get("toolUseResult")
    for index, block in enumerate(blocks):
        if not is_object_dict(block) or block.get("type") != "tool_result":
            continue
        call_id = block.get("tool_use_id")
        if not isinstance(call_id, str):
            entries.append(_missing(row, "tool_result.tool_use_id"))
            continue
        key = f"claude:{row.uuid}" if len(blocks) == 1 else f"claude:{row.uuid}:{index}"
        started = call_ms.pop(call_id, None)
        entries.append(
            ToolResult(
                **_entry_fields(row, index * 10, key=key),
                call_id=call_id,
                content=_text(block.get("content")) or "",
                status=_status(block, raw, result),
                duration_ms=None if started is None else max(row.ms - started, 0),
            )
        )
        agent = result.get("agentId") if is_object_dict(result) else None
        if isinstance(agent, str):
            entries.append(
                SubagentLink(
                    **_entry_fields(row, index * 10 + 1, key=f"{key}:link"),
                    call_id=call_id,
                    child_record=agent,
                )
            )
    return ParseState(call_ms), tuple(entries)


def _status(block: Mapping[str, object], raw: Mapping[str, object], result: object) -> ToolStatus:
    if block.get("is_error") is True:
        return ToolStatus.DENIED if raw.get("toolDenialKind") else ToolStatus.ERROR
    if is_object_dict(result) and (result.get("isAsync") is True or "backgroundTaskId" in result):
        return ToolStatus.BACKGROUND
    return ToolStatus.OK
