from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from base.lm.harness.events import (
    AssistantText,
    BridgeEvent,
    TextDelta,
    ToolResult,
    ToolUse,
)

# meiva:note
# ACP carries committed semantic blocks, not provider deltas: one
# `agent_message_chunk` is a whole segment of the assistant's answer, and the
# runtime never streams raw tokens. So the bridge emits `TextDelta` per chunk for
# liveness and one `AssistantText` per turn for the answer, and it never pretends
# to have token-level granularity it was not given.
#
# `agent_thought_chunk` maps to nothing on purpose: a thought is not output, and
# folding it into the answer text would put the model's scratchpad in front of the
# user. It is still visible in the runtime's own log for postmortems.
#
# Tool results are the one payload that can be megabytes, so the event carries a
# bounded preview — the model already has the full content through its own turn.
# meiva:link base/lm/harness/deepseek/docs/deepseek.meiva.okf.md
TOOL_RESULT_PREVIEW_CHARS = 2000
_FAILED_TOOL_STATUSES = frozenset({"failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class PromptFields:
    stop_reason: str
    subtype: str
    is_error: bool
    interrupted: bool


def map_update(update: Mapping[str, Any], *, turn_id: str) -> list[BridgeEvent]:
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        text = _text_of(update.get("content"))
        return [TextDelta(turn_id=turn_id, text=text)] if text else []
    if kind == "tool_call":
        return [_tool_use(update, turn_id=turn_id)]
    if kind == "tool_call_update":
        return _tool_result(update, turn_id=turn_id)
    return []


def assistant_text(text: str, *, turn_id: str) -> AssistantText | None:
    trimmed = text.strip()
    return AssistantText(turn_id=turn_id, text=trimmed) if trimmed else None


def parse_prompt_result(result: Mapping[str, Any]) -> PromptFields:
    raw = result.get("stopReason")
    stop_reason = raw if isinstance(raw, str) and raw else "unknown"
    return PromptFields(
        stop_reason=stop_reason,
        subtype=_SUBTYPES.get(stop_reason, stop_reason),
        is_error=stop_reason != "end_turn",
        interrupted=stop_reason == "cancelled",
    )


# meiva:note
# The five ACP v1 stop reasons, with anything newer treated as a failure the upper
# layer can still name. That is the opposite of silently reporting success: an
# unrecognized ending must not be priced or reported as a completed turn.
_SUBTYPES = {
    "end_turn": "success",
    "cancelled": "cancelled",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "max_turn_requests": "max_turn_requests",
}


def _tool_use(update: Mapping[str, Any], *, turn_id: str) -> ToolUse:
    raw_input = update.get("rawInput")
    return ToolUse(
        turn_id=turn_id,
        tool_use_id=str(update.get("toolCallId", "")),
        name=str(update.get("title") or update.get("kind") or "tool"),
        tool_input=cast("dict[str, Any]", raw_input) if isinstance(raw_input, dict) else {},
    )


def _tool_result(update: Mapping[str, Any], *, turn_id: str) -> list[BridgeEvent]:
    status = str(update.get("status", ""))
    if status not in {"completed", "failed", "cancelled"}:
        return []
    return [
        ToolResult(
            turn_id=turn_id,
            tool_use_id=str(update.get("toolCallId", "")),
            is_error=status in _FAILED_TOOL_STATUSES,
            content=_preview(_content_text(update.get("content"))),
        )
    ]


def _content_text(blocks: object) -> str:
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in cast("list[object]", blocks):
        if not isinstance(block, dict):
            continue
        inner = cast("dict[str, Any]", block).get("content")
        if isinstance(inner, dict):
            text = _text_of(cast("dict[str, Any]", inner))
            if text:
                parts.append(text)
    return "\n".join(parts)


def _text_of(block: object) -> str:
    if not isinstance(block, dict):
        return ""
    payload = cast("dict[str, Any]", block)
    if payload.get("type") != "text":
        return ""
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def _preview(text: str) -> str:
    if len(text) <= TOOL_RESULT_PREVIEW_CHARS:
        return text
    return text[:TOOL_RESULT_PREVIEW_CHARS] + f"… (+{len(text) - TOOL_RESULT_PREVIEW_CHARS} chars)"
