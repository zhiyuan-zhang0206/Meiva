from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

from base.lm.harness.events import (
    AssistantText,
    BridgeEvent,
    SessionStarted,
    TextDelta,
    ToolResult,
    ToolUse,
    Usage,
)


# meiva:note
# One `result` event, decoded. `denied_actions` is the only structured signal
# that a tool call was refused: the CLI still reports `status: SUCCESS` and
# exits 0, so a caller that reads `status` alone records a silent no-op as a
# successful turn.
@dataclass(frozen=True, slots=True)
class ResultFields:
    session_id: str | None
    status: str
    text: str
    error: str | None
    num_turns: int
    duration_ms: int
    usage: Usage = field(default_factory=Usage)
    denied_actions: tuple[str, ...] = ()

    @property
    def denied(self) -> bool:
        return bool(self.denied_actions)

    @property
    def interrupted(self) -> bool:
        return self.error == "interrupted"

    @property
    def is_error(self) -> bool:
        return self.status != "SUCCESS" or self.error is not None or self.denied

    @property
    def subtype(self) -> str:
        if self.denied:
            return "denied"
        if self.interrupted:
            return "interrupted"
        return self.status.lower()


def map_init(payload: dict[str, Any]) -> SessionStarted:
    init = _mapping(payload.get("init"))
    return SessionStarted(
        session_id=_text(payload.get("conversation_id")) or "",
        model=_text(init.get("model")),
        cwd=_text(init.get("cwd")),
        api_key_source=_text(init.get("permission_mode")),
    )


# meiva:note
# Translate one `step_update`; text accumulation stays with the session.
def map_step_update(update: dict[str, Any], *, turn_id: str) -> tuple[BridgeEvent, ...]:
    step_type = _text(update.get("step_type"))
    state = _text(update.get("state"))
    delta = _text(update.get("text_delta"))
    if step_type == "agent_response" and delta:
        return (TextDelta(turn_id=turn_id, text=delta),)
    if step_type != "tool":
        return ()
    info = _mapping(update.get("tool_info"))
    name = _text(update.get("tool_name")) or _text(info.get("name")) or "unknown"
    tool_use_id = f"{turn_id}#{update.get('step_index', 0)}"
    if state == "ACTIVE":
        return (
            ToolUse(
                turn_id=turn_id,
                tool_use_id=tool_use_id,
                name=name,
                tool_input=dict(_mapping(info.get("parameters"))),
            ),
        )
    if state == "DONE":
        return (
            ToolResult(
                turn_id=turn_id,
                tool_use_id=tool_use_id,
                is_error=_tool_failed(info),
                content=_tool_content(info),
            ),
        )
    return ()


def assistant_text(update: dict[str, Any], *, turn_id: str, text: str) -> AssistantText | None:
    if _text(update.get("step_type")) != "agent_response" or _text(update.get("state")) != "DONE":
        return None
    return AssistantText(turn_id=turn_id, text=text)


# meiva:note
# `input_tokens` is the whole prompt here too: the CLI's recorded `total_tokens`
# is input + output, with `cache_read_tokens` inside the input figure rather than
# beside it. Stated so the next reader does not add the cache count twice; see
# `Usage` in `base/lm/harness/events.py`.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def parse_usage(payload: Any) -> Usage:
    usage = _mapping(payload)
    output = _int(usage.get("output_tokens"))
    # meiva:note
    # `agy` bills thinking tokens as output and reports them separately; fold
    # them in so a turn's output figure stays comparable with the other
    # harnesses.
    output += _int(usage.get("thinking_tokens"))
    return Usage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=output,
        cache_read_input_tokens=_int(usage.get("cache_read_tokens")),
    )


def parse_result(payload: dict[str, Any]) -> ResultFields:
    denied = tuple(
        _denial_label(entry)
        for entry in cast("list[Any]", payload.get("denied_actions") or [])
        if isinstance(entry, dict)
    )
    duration = payload.get("duration_seconds")
    return ResultFields(
        session_id=_text(payload.get("conversation_id")),
        status=_text(payload.get("status")) or "UNKNOWN",
        text=_text(payload.get("response")) or "",
        error=_text(payload.get("error")),
        num_turns=_int(payload.get("num_turns")),
        duration_ms=int(float(duration) * 1000.0) if isinstance(duration, int | float) else 0,
        usage=parse_usage(payload.get("usage")),
        denied_actions=denied,
    )


def _denial_label(entry: Any) -> str:
    item = _mapping(entry)
    action = _text(item.get("action")) or "unknown"
    display = _text(item.get("display_name")) or ""
    return f"{action}:{display}".rstrip(":")


def _tool_failed(info: dict[str, Any]) -> bool:
    # meiva:note
    # `agy` does not mark a failing shell command: a non-zero exit arrives as
    # plain output text. Only a tool-level error field is trustworthy here.
    return bool(info.get("error") or info.get("Error"))


def _tool_content(info: dict[str, Any]) -> str:
    for key in ("output", "Output", "combinedOutput", "combined_output"):
        value = info.get(key)
        if isinstance(value, str):
            return value
        if value is not None:
            return json.dumps(value, ensure_ascii=False)
    return ""


def _mapping(value: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0
