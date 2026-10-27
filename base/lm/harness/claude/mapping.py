from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from base.lm.harness.events import (
    AssistantText,
    BridgeEvent,
    RateLimitObserved,
    ReasoningDelta,
    ReasoningText,
    SessionStarted,
    TextDelta,
    ToolResult,
    ToolUse,
    UnsolicitedOutput,
    Usage,
)

type _Json = str | int | float | bool | dict[str, Any] | list[_Json] | None

# meiva:note
# Dispatch is by class name rather than `isinstance`: the SDK is injected, and a
# test fake carries the same class names and fields. An unknown message maps to
# nothing, which is how a newer SDK stays compatible. Tool results are the one
# payload that can be megabytes, so the event carries a preview — the model
# already has the full content through the SDK's own transcript.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
TOOL_RESULT_PREVIEW_CHARS = 2000


@dataclass(frozen=True, slots=True)
class ResultFields:
    session_id: str | None
    subtype: str
    is_error: bool
    stop_reason: str | None
    num_turns: int
    duration_ms: int
    usage: Usage
    cost_usd: float | None
    result_text: str | None
    permission_denials: tuple[str, ...]
    # meiva:note
    # The CLI names what started the turn a result reports on: `human` for a prompt
    # this session submitted, `task-notification` for the CLI's own turn over a
    # background task it finished without us, and nothing at all on a CLI too old to
    # name it. `_claim` is what decides which of those can be our turn's boundary.
    # meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
    origin_kind: str | None


def map_message(message: object, *, turn_id: str | None) -> list[BridgeEvent]:
    handler = _HANDLERS.get(type(message).__name__)
    if handler is None:
        return []
    return handler(message, turn_id)


def parse_result(message: object) -> ResultFields:
    return ResultFields(
        session_id=_text(getattr(message, "session_id", None)),
        subtype=_text(getattr(message, "subtype", None)) or "unknown",
        is_error=bool(getattr(message, "is_error", False)),
        stop_reason=_text(getattr(message, "stop_reason", None)),
        num_turns=int(getattr(message, "num_turns", 0) or 0),
        duration_ms=int(getattr(message, "duration_ms", 0) or 0),
        usage=_usage(getattr(message, "usage", None)),
        cost_usd=_float_or_none(getattr(message, "total_cost_usd", None)),
        result_text=_text(getattr(message, "result", None)),
        permission_denials=_denials(getattr(message, "permission_denials", None)),
        origin_kind=_origin_kind(getattr(message, "origin", None)),
    )


def _system(message: object, turn_id: str | None) -> list[BridgeEvent]:
    if getattr(message, "subtype", None) != "init":
        return []
    data = _as_dict(getattr(message, "data", None))
    return [
        SessionStarted(
            session_id=str(data.get("session_id", "")),
            model=_text(data.get("model")),
            cwd=_text(data.get("cwd")),
            api_key_source=_text(data.get("apiKeySource")),
            turn_id=turn_id,
        )
    ]


def _stream(message: object, turn_id: str | None) -> list[BridgeEvent]:
    if turn_id is None:
        return []
    event = _as_dict(getattr(message, "event", None))
    if event.get("type") != "content_block_delta":
        return []
    delta = _as_dict(event.get("delta"))
    delta_type = delta.get("type")
    if delta_type == "thinking_delta":
        thinking = delta.get("thinking")
        if not isinstance(thinking, str) or not thinking:
            return []
        return [ReasoningDelta(turn_id=turn_id, text=thinking)]
    if delta_type != "text_delta":
        return []
    text = delta.get("text")
    if not isinstance(text, str) or not text:
        return []
    return [TextDelta(turn_id=turn_id, text=text)]


def _assistant(message: object, turn_id: str | None) -> list[BridgeEvent]:
    if turn_id is None:
        return []
    events: list[BridgeEvent] = []
    for block in _as_list(getattr(message, "content", None)):
        kind = type(block).__name__
        if kind == "ThinkingBlock":
            thinking = _text(getattr(block, "thinking", None))
            if thinking:
                events.append(ReasoningText(turn_id=turn_id, text=thinking))
        elif kind == "TextBlock":
            text = _text(getattr(block, "text", None))
            if text:
                events.append(AssistantText(turn_id=turn_id, text=text))
        elif kind == "ToolUseBlock":
            events.append(
                ToolUse(
                    turn_id=turn_id,
                    tool_use_id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    tool_input=_dict(getattr(block, "input", None)),
                )
            )
    return events


def _user(message: object, turn_id: str | None) -> list[BridgeEvent]:
    if turn_id is None:
        return []
    events: list[BridgeEvent] = []
    for block in _as_list(getattr(message, "content", None)):
        if type(block).__name__ != "ToolResultBlock":
            continue
        events.append(
            ToolResult(
                turn_id=turn_id,
                tool_use_id=str(getattr(block, "tool_use_id", "")),
                is_error=bool(getattr(block, "is_error", False)),
                content=_render(getattr(block, "content", None)),
            )
        )
    return events


def _rate_limit(message: object, turn_id: str | None) -> list[BridgeEvent]:
    info = getattr(message, "rate_limit_info", None)
    if info is None:
        return []
    return [
        RateLimitObserved(
            status=str(getattr(info, "status", "unknown")),
            utilization=_float_or_none(getattr(info, "utilization", None)),
            resets_at=_int_or_none(getattr(info, "resets_at", None)),
            rate_limit_type=_text(getattr(info, "rate_limit_type", None)),
            turn_id=turn_id,
        )
    ]


# meiva:note
# A background task the CLI finished reports itself with a `task_notification`
# system message, which is a `SystemMessage` subclass whose class name is its own —
# the `SystemMessage` entry above therefore never sees it. The report is content
# that belongs to no turn of ours: the CLI either folded it into the turn that was
# running (that turn's model already read it) or answered it in a turn of its own.
# Either way it is the agent's to read, so it maps to `UnsolicitedOutput` and the
# session decides whether anyone has seen it yet.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
def _task_notification(message: object, turn_id: str | None) -> list[BridgeEvent]:
    del turn_id
    task_id = _text(getattr(message, "task_id", None)) or "unknown"
    status = _text(getattr(message, "status", None)) or "unknown"
    lines = [f"Background task {task_id} reported {status}."]
    summary = _text(getattr(message, "summary", None))
    if summary:
        lines.append(summary)
    output_file = _text(getattr(message, "output_file", None))
    if output_file:
        lines.append(f"Output file: {output_file}")
    return [UnsolicitedOutput(source="task-notification", text="\n".join(lines))]


# meiva:note
# The status a `task_updated` message reports, or None when it reports none: the
# adapter stops tracking the task it names, so a task that never reported `completed`
# is still stopped by `close()` rather than left running under a dead session.
# meiva:link base/lm/harness/claude/docs/background-tasks.meiva.okf.md
def task_update_status(message: object) -> str | None:
    return _text(_as_dict(getattr(message, "patch", None)).get("status"))


def _origin_kind(raw: _Json) -> str | None:
    kind = _as_dict(raw).get("kind")
    return kind if isinstance(kind, str) else None


_HANDLERS = {
    "SystemMessage": _system,
    "TaskNotificationMessage": _task_notification,
    "StreamEvent": _stream,
    "AssistantMessage": _assistant,
    "UserMessage": _user,
    "RateLimitEvent": _rate_limit,
}


# meiva:note
# Anthropic reports `input_tokens` as what was billed at the full rate: the
# uncached remainder. `cache_read_input_tokens` and `cache_creation_input_tokens`
# are charged separately and sit outside it, so the turn's total input is their
# sum — a live turn read as 8 in, 17391 cache-read, 6621 cache-write, which is a
# 24020-token prompt. Reporting the remainder alone understates the prompt and
# trips the pricing invariant that cached tokens cannot exceed input.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def _usage(raw: _Json) -> Usage:
    data = _as_dict(raw)
    cache_read = _int_or_zero(data.get("cache_read_input_tokens"))
    cache_creation = _int_or_zero(data.get("cache_creation_input_tokens"))
    return Usage(
        input_tokens=_int_or_zero(data.get("input_tokens")) + cache_read + cache_creation,
        output_tokens=_int_or_zero(data.get("output_tokens")),
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


def _denials(raw: _Json) -> tuple[str, ...]:
    names: list[str] = []
    for item in _as_list(raw):
        if isinstance(item, dict):
            data = _as_dict(item)
            names.append(str(data.get("tool_name") or data.get("name") or data))
        else:
            names.append(str(item))
    return tuple(names)


def _render(content: _Json) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return _clip(content)
    blocks = _as_list(content)
    if blocks:
        parts: list[str] = []
        for block in blocks:
            if isinstance(block, dict):
                data = _as_dict(block)
                text = data.get("text")
                parts.append(text if isinstance(text, str) else str(data.get("type", "")))
            else:
                parts.append(str(block))
        return _clip("\n".join(part for part in parts if part))
    return _clip(str(content))


def _clip(text: str) -> str:
    if len(text) <= TOOL_RESULT_PREVIEW_CHARS:
        return text
    return text[:TOOL_RESULT_PREVIEW_CHARS] + "..."


def _dict(value: _Json) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"value": value}


def _as_dict(value: _Json) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: _Json) -> list[_Json]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
