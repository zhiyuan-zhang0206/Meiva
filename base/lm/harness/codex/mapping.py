from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from base.lm.harness.events import (
    BridgeEvent,
    ReasoningDelta,
    ReasoningText,
    TextDelta,
    ToolResult,
    ToolUse,
    Usage,
)

type JsonValue = (
    str | int | float | bool | dict[str, Any] | list[JsonValue] | Mapping[str, Any] | None
)
type JsonObject = dict[str, Any]
type RequestParams = Mapping[str, Any]


def _as_object(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _as_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


# meiva:note
# Codex speaks ThreadItem, not the bridge's own vocabulary: a `commandExecution`
# item is one tool use whose result arrives as the same item completed, and a
# `fileChange` item is an apply-patch. Everything else (reasoning, plan, images,
# sub-agent activity) is surface the bridge does not model, so it maps to nothing
# rather than to a synthetic event. Tool results are the one payload that can be
# megabytes, so the event carries a preview.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
TOOL_RESULT_PREVIEW_CHARS = 2000


def text_delta(params: RequestParams, turn_id: str) -> list[BridgeEvent]:
    delta = _as_text(params.get("delta"))
    if not delta:
        return []
    return [TextDelta(turn_id=turn_id, text=delta)]


def reasoning_delta(params: RequestParams, turn_id: str) -> list[BridgeEvent]:
    delta = _as_text(params.get("delta"))
    if not delta:
        return []
    return [ReasoningDelta(turn_id=turn_id, text=delta)]


def item_event(params: RequestParams, *, phase: str, turn_id: str) -> list[BridgeEvent]:
    item = _as_object(params.get("item"))
    if not item:
        return []
    kind = _as_text(item.get("type"))
    if kind == "reasoning":
        return _reasoning(item, phase, turn_id)
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


def _reasoning(item: RequestParams, phase: str, turn_id: str) -> list[BridgeEvent]:
    if phase != "completed":
        return []
    for field in ("summary", "content"):
        raw: JsonValue = item.get(field)
        if isinstance(raw, list):
            parts = [s.strip() for s in raw if isinstance(s, str) and s.strip()]
            if parts:
                return [ReasoningText(turn_id=turn_id, text="\n\n".join(parts))]
        elif isinstance(raw, str) and raw.strip():
            return [ReasoningText(turn_id=turn_id, text=raw.strip())]
    return []


def assistant_text(item: RequestParams, *, final_only: bool = False) -> str | None:
    if _as_text(item.get("type")) != "agentMessage":
        return None
    if final_only and item.get("phase") not in {None, "final_answer"}:
        return None
    text = _as_text(item.get("text"))
    return text or None


# meiva:note
# `inputTokens` already totals the prompt: `cachedInputTokens` is a subset of it,
# as the CLI's own `total_tokens` (input + output) shows, so nothing is added here.
# The other two adapters translate into the same shape; see `Usage` in
# `base/lm/harness/events.py`.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def usage_from(params: RequestParams) -> Usage:
    last = _as_object(_as_object(params.get("tokenUsage")).get("last"))
    if not last:
        return Usage()
    return Usage(
        input_tokens=_as_int(last.get("inputTokens")) or 0,
        output_tokens=_as_int(last.get("outputTokens")) or 0,
        cache_creation_input_tokens=_as_int(last.get("cacheWriteInputTokens")) or 0,
        cache_read_input_tokens=_as_int(last.get("cachedInputTokens")) or 0,
    )


@dataclass(frozen=True, slots=True)
class TurnFields:
    status: str
    error_message: str | None
    duration_ms: int

    @classmethod
    def parse(cls, turn: RequestParams) -> TurnFields:
        return cls(
            status=_as_text(turn.get("status")) or "unknown",
            error_message=_as_text(_as_object(turn.get("error")).get("message")),
            duration_ms=_as_int(turn.get("durationMs")) or 0,
        )


def approval_denial(method: str, params: RequestParams) -> str:
    if method == "item/commandExecution/requestApproval":
        command = _as_text(params.get("command"))
        return f"command: {command}" if command else "command"
    if method == "item/fileChange/requestApproval":
        return "file change"
    if method == "item/permissions/requestApproval":
        return "permissions"
    return method


def _command(item: RequestParams, phase: str, turn_id: str) -> list[BridgeEvent]:
    identifier = _as_text(item.get("id")) or ""
    if phase == "started":
        return [
            ToolUse(
                turn_id=turn_id,
                tool_use_id=identifier,
                name="command",
                tool_input={
                    "command": _as_text(item.get("command")) or "",
                    "cwd": item.get("cwd"),
                },
            )
        ]
    status = _as_text(item.get("status")) or ""
    exit_code = _as_int(item.get("exitCode"))
    failed = status in {"failed", "declined"} or (exit_code is not None and exit_code != 0)
    return [
        ToolResult(
            turn_id=turn_id,
            tool_use_id=identifier,
            is_error=failed,
            content=_clip(_as_text(item.get("aggregatedOutput")) or ""),
        )
    ]


def _file_change(item: RequestParams, phase: str, turn_id: str) -> list[BridgeEvent]:
    identifier = _as_text(item.get("id")) or ""
    raw_changes = item.get("changes")
    paths = _paths(raw_changes)
    changes = _changes(raw_changes)
    if phase == "started":
        tool_input: dict[str, object] = {"paths": paths}
        if changes:
            tool_input["changes"] = changes
        return [
            ToolUse(
                turn_id=turn_id,
                tool_use_id=identifier,
                name="patch",
                tool_input=tool_input,
            )
        ]
    status = _as_text(item.get("status")) or ""
    return [
        ToolResult(
            turn_id=turn_id,
            tool_use_id=identifier,
            is_error=status != "completed",
            content=_clip(json.dumps({"changes": paths, "status": status}, separators=(",", ":"))),
        )
    ]


def _mcp_call(item: RequestParams, phase: str, turn_id: str) -> list[BridgeEvent]:
    identifier = _as_text(item.get("id")) or ""
    name = f"mcp:{_as_text(item.get('server'))}/{_as_text(item.get('tool'))}"
    if phase == "started":
        return [
            ToolUse(
                turn_id=turn_id,
                tool_use_id=identifier,
                name=name,
                tool_input=_arguments(item.get("arguments")),
            )
        ]
    status = _as_text(item.get("status")) or ""
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


def _dynamic_tool(item: RequestParams, phase: str, turn_id: str) -> list[BridgeEvent]:
    identifier = _as_text(item.get("id")) or ""
    if phase == "started":
        return [
            ToolUse(
                turn_id=turn_id,
                tool_use_id=identifier,
                name=_as_text(item.get("tool")) or "dynamic",
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


def _web_search(item: RequestParams, phase: str, turn_id: str) -> list[BridgeEvent]:
    if phase != "started":
        return []
    return [
        ToolUse(
            turn_id=turn_id,
            tool_use_id=_as_text(item.get("id")) or "",
            name="web_search",
            tool_input={"query": item.get("query")},
        )
    ]


def _paths(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        return []
    paths: list[str] = []
    for entry in value:
        path = _as_text(_as_object(entry).get("path"))
        if path is not None:
            paths.append(path)
    return paths


def _changes(value: JsonValue) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    changes: list[dict[str, object]] = []
    has_detail = False
    for entry in value:
        obj = _as_object(entry)
        path = _as_text(obj.get("path"))
        if path is None:
            continue
        change: dict[str, object] = {"path": path}
        kind_raw = obj.get("kind")
        kind = _as_text(kind_raw) or _as_text(_as_object(kind_raw).get("type"))
        if kind is not None:
            change["kind"] = kind
            has_detail = True
        diff = _as_text(obj.get("diff")) or _as_text(obj.get("patch"))
        if diff is not None:
            change["diff"] = diff
            has_detail = True
        changes.append(change)
    return changes if has_detail else []


def _arguments(value: JsonValue) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {"arguments": value}


def _clip(text: str) -> str:
    if len(text) <= TOOL_RESULT_PREVIEW_CHARS:
        return text
    return text[:TOOL_RESULT_PREVIEW_CHARS] + "..."
