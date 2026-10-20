from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from base.lm.harness.codex.transport import JsonObject, as_int, as_object, as_text
from base.lm.harness.events import (
    BridgeEvent,
    TextDelta,
    ToolResult,
    ToolUse,
    Usage,
)

# meiva:note
# Codex speaks ThreadItem, not the bridge's own vocabulary: a `commandExecution`
# item is one tool use whose result arrives as the same item completed, and a
# `fileChange` item is an apply-patch. Everything else (reasoning, plan, images,
# sub-agent activity) is surface the bridge does not model, so it maps to nothing
# rather than to a synthetic event. Tool results are the one payload that can be
# megabytes, so the event carries a preview.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
TOOL_RESULT_PREVIEW_CHARS = 2000


def text_delta(params: Mapping[str, object], turn_id: str) -> list[BridgeEvent]:
    delta = as_text(params.get("delta"))
    if not delta:
        return []
    return [TextDelta(turn_id=turn_id, text=delta)]


def item_event(params: Mapping[str, object], *, phase: str, turn_id: str) -> list[BridgeEvent]:
    item = as_object(params.get("item"))
    if not item:
        return []
    kind = as_text(item.get("type"))
    if kind == "commandExecution":
        return _command(item, phase, turn_id)
    if kind == "fileChange":
        return _file_change(item, phase, turn_id)
    if kind == "mcpToolCall":
        return _mcp_call(item, phase, turn_id)
    if kind == "dynamicToolCall":
        return _dynamic_tool(item, phase, turn_id)
    if kind == "webSearch":
        return _web_search(item, phase, turn_id)
    return []


def assistant_text(item: Mapping[str, object]) -> str | None:
    if as_text(item.get("type")) != "agentMessage":
        return None
    text = as_text(item.get("text"))
    return text or None


def is_final_answer(item: Mapping[str, object]) -> bool:
    phase = item.get("phase")
    return phase is None or phase == "final_answer"


# meiva:note
# `inputTokens` already totals the prompt: `cachedInputTokens` is a subset of it,
# as the CLI's own `total_tokens` (input + output) shows, so nothing is added here.
# The other two adapters translate into the same shape; see `Usage` in
# `base/lm/harness/events.py`.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def usage_from(params: Mapping[str, object]) -> Usage:
    last = as_object(as_object(params.get("tokenUsage")).get("last"))
    if not last:
        return Usage()
    return Usage(
        input_tokens=as_int(last.get("inputTokens")) or 0,
        output_tokens=as_int(last.get("outputTokens")) or 0,
        cache_creation_input_tokens=as_int(last.get("cacheWriteInputTokens")) or 0,
        cache_read_input_tokens=as_int(last.get("cachedInputTokens")) or 0,
    )


@dataclass(frozen=True, slots=True)
class TurnFields:
    status: str
    error_message: str | None
    duration_ms: int


def parse_turn(turn: Mapping[str, object]) -> TurnFields:
    return TurnFields(
        status=as_text(turn.get("status")) or "unknown",
        error_message=as_text(as_object(turn.get("error")).get("message")),
        duration_ms=as_int(turn.get("durationMs")) or 0,
    )


def approval_denial(method: str, params: Mapping[str, object]) -> str:
    if method == "item/commandExecution/requestApproval":
        command = as_text(params.get("command"))
        return f"command: {command}" if command else "command"
    if method == "item/fileChange/requestApproval":
        return "file change"
    if method == "item/permissions/requestApproval":
        return "permissions"
    return method


def _command(item: Mapping[str, object], phase: str, turn_id: str) -> list[BridgeEvent]:
    identifier = as_text(item.get("id")) or ""
    if phase == "started":
        return [
            ToolUse(
                turn_id=turn_id,
                tool_use_id=identifier,
                name="command",
                tool_input={
                    "command": as_text(item.get("command")) or "",
                    "cwd": item.get("cwd"),
                },
            )
        ]
    status = as_text(item.get("status")) or ""
    exit_code = as_int(item.get("exitCode"))
    failed = status in {"failed", "declined"} or (exit_code is not None and exit_code != 0)
    return [
        ToolResult(
            turn_id=turn_id,
            tool_use_id=identifier,
            is_error=failed,
            content=_clip(as_text(item.get("aggregatedOutput")) or ""),
        )
    ]


def _file_change(item: Mapping[str, object], phase: str, turn_id: str) -> list[BridgeEvent]:
    identifier = as_text(item.get("id")) or ""
    paths = _paths(item.get("changes"))
    if phase == "started":
        return [
            ToolUse(
                turn_id=turn_id,
                tool_use_id=identifier,
                name="patch",
                tool_input={"paths": paths},
            )
        ]
    status = as_text(item.get("status")) or ""
    return [
        ToolResult(
            turn_id=turn_id,
            tool_use_id=identifier,
            is_error=status != "completed",
            content=_clip(json.dumps({"changes": paths, "status": status}, separators=(",", ":"))),
        )
    ]


def _mcp_call(item: Mapping[str, object], phase: str, turn_id: str) -> list[BridgeEvent]:
    identifier = as_text(item.get("id")) or ""
    name = f"mcp:{as_text(item.get('server'))}/{as_text(item.get('tool'))}"
    if phase == "started":
        return [
            ToolUse(
                turn_id=turn_id,
                tool_use_id=identifier,
                name=name,
                tool_input=_arguments(item.get("arguments")),
            )
        ]
    status = as_text(item.get("status")) or ""
    payload: object = item.get("result")
    if payload is None:
        payload = item.get("error")
    return [
        ToolResult(
            turn_id=turn_id,
            tool_use_id=identifier,
            is_error=status != "completed" or item.get("error") is not None,
            content=_clip(json.dumps(payload, ensure_ascii=False)),
        )
    ]


def _dynamic_tool(item: Mapping[str, object], phase: str, turn_id: str) -> list[BridgeEvent]:
    identifier = as_text(item.get("id")) or ""
    if phase == "started":
        return [
            ToolUse(
                turn_id=turn_id,
                tool_use_id=identifier,
                name=as_text(item.get("tool")) or "dynamic",
                tool_input=_arguments(item.get("arguments")),
            )
        ]
    return [
        ToolResult(
            turn_id=turn_id,
            tool_use_id=identifier,
            is_error=item.get("success") is not True,
            content=_clip(json.dumps(item.get("contentItems") or [], ensure_ascii=False)),
        )
    ]


def _web_search(item: Mapping[str, object], phase: str, turn_id: str) -> list[BridgeEvent]:
    if phase != "started":
        return []
    return [
        ToolUse(
            turn_id=turn_id,
            tool_use_id=as_text(item.get("id")) or "",
            name="web_search",
            tool_input={"query": item.get("query")},
        )
    ]


def _paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    entries = cast("list[object]", value)
    paths: list[str] = []
    for entry in entries:
        path = as_text(as_object(entry).get("path"))
        if path is not None:
            paths.append(path)
    return paths


def _arguments(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(cast("JsonObject", value))
    return {"arguments": value}


def _clip(text: str) -> str:
    if len(text) <= TOOL_RESULT_PREVIEW_CHARS:
        return text
    return text[:TOOL_RESULT_PREVIEW_CHARS] + "..."
