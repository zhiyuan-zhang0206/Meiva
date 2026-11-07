from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.host.env.harness_binaries import resolve_user_home
from base.lm.harness.events import (
    AssistantText,
    BridgeEvent,
    ReasoningText,
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


def map_init(payload: Mapping[str, object]) -> SessionStarted:
    init = _mapping(payload.get("init"))
    return SessionStarted(
        session_id=_text(payload.get("conversation_id")) or "",
        model=_text(init.get("model")),
        cwd=_text(init.get("cwd")),
        api_key_source=_text(init.get("permission_mode")),
    )


# meiva:note
# Translate one `step_update`; text accumulation stays with the session.
def map_step_update(update: Mapping[str, object], *, turn_id: str) -> tuple[BridgeEvent, ...]:
    step_type = _text(update.get("step_type"))
    if step_type == "agent_response":
        return _map_agent_response_step(update, turn_id=turn_id)
    if step_type == "tool":
        return _map_tool_step(update, turn_id=turn_id)
    return ()


def _map_agent_response_step(
    update: Mapping[str, object], *, turn_id: str
) -> tuple[BridgeEvent, ...]:
    state = _text(update.get("state"))
    delta = _text(update.get("text_delta"))
    step_index = _int(update.get("step_index"))
    conversation_id = _text(update.get("conversation_id"))
    events: list[BridgeEvent] = []
    if state == "DONE":
        thinking = _text(update.get("thinking")) or (
            _transcript_thinking(conversation_id, step_index, before_tool=False)
            if conversation_id
            else None
        )
        if thinking:
            events.append(ReasoningText(turn_id=turn_id, text=thinking))
    if delta:
        events.append(TextDelta(turn_id=turn_id, text=delta))
    return tuple(events)


def _map_tool_step(update: Mapping[str, object], *, turn_id: str) -> tuple[BridgeEvent, ...]:
    state = _text(update.get("state"))
    step_index = _int(update.get("step_index"))
    conversation_id = _text(update.get("conversation_id"))
    info = _mapping(update.get("tool_info"))
    name = _text(update.get("tool_name")) or _text(info.get("name")) or "unknown"
    tool_use_id = f"{turn_id}#{update.get('step_index', 0)}"
    if state == "ACTIVE":
        params = dict(_mapping(info.get("parameters")))
        if conversation_id:
            params = {
                **_transcript_tool_args(conversation_id, step_index, name),
                **params,
            }
        thinking = _text(update.get("thinking")) or (
            _transcript_thinking(conversation_id, step_index, before_tool=True)
            if conversation_id
            else None
        )
        tool_use = ToolUse(
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            name=name,
            tool_input=params,
        )
        if thinking:
            return (ReasoningText(turn_id=turn_id, text=thinking), tool_use)
        return (tool_use,)
    if state == "DONE":
        content = _tool_content(info)
        if not content and conversation_id:
            content = _transcript_tool_output(conversation_id, step_index)
        return (
            ToolResult(
                turn_id=turn_id,
                tool_use_id=tool_use_id,
                is_error=_tool_failed(info),
                content=content,
            ),
        )
    return ()


def assistant_text(
    update: Mapping[str, object], *, turn_id: str, text: str
) -> AssistantText | None:
    if _text(update.get("step_type")) != "agent_response" or _text(update.get("state")) != "DONE":
        return None
    return AssistantText(turn_id=turn_id, text=text)


# meiva:note
# `input_tokens` is the whole prompt here too: the CLI's recorded `total_tokens`
# is input + output, with `cache_read_tokens` inside the input figure rather than
# beside it. Stated so the next reader does not add the cache count twice; see
# `Usage` in `base/lm/harness/events.py`.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def parse_usage(payload: object) -> Usage:
    usage = _mapping(payload)
    raw_in = _int(usage.get("input_tokens"))
    raw_out = _int(usage.get("output_tokens"))
    thinking = _int(usage.get("thinking_tokens"))
    cache_read = _int(usage.get("cache_read_tokens"))
    total = _int(usage.get("total_tokens"))
    # meiva:note
    # `agy` bills thinking tokens as output and reports them separately; fold
    # them in so a turn's output figure stays comparable with the other
    # harnesses. When the CLI reports `total_tokens == input_tokens + output_tokens`
    # or `cache_read_tokens > input_tokens`, `input_tokens` is the uncached portion
    # and `cache_read_tokens` sits outside it.
    input_tokens = (
        raw_in + cache_read
        if cache_read > raw_in or (cache_read > 0 and total > 0 and total == raw_in + raw_out)
        else raw_in
    )
    return Usage(
        input_tokens=input_tokens,
        output_tokens=raw_out + thinking,
        cache_read_input_tokens=cache_read,
    )


def parse_result(payload: Mapping[str, object], *, home: Path | None = None) -> ResultFields:
    raw_denied = payload.get("denied_actions")
    denied_items = raw_denied if is_object_list(raw_denied) else []
    denied = tuple(_denial_label(entry) for entry in denied_items if is_object_dict(entry))
    duration = payload.get("duration_seconds")
    session_id = _text(payload.get("conversation_id"))
    status = _text(payload.get("status")) or "UNKNOWN"
    error = _text(payload.get("error"))
    if (
        status != "SUCCESS"
        and error != "interrupted"
        and _transcript_turn_recovered(session_id, home=home)
    ):
        status = "SUCCESS"
        error = None
    if status == "SUCCESS" and error is not None and error != "interrupted":
        error = None
    return ResultFields(
        session_id=session_id,
        status=status,
        text=_text(payload.get("response")) or "",
        error=error,
        num_turns=_int(payload.get("num_turns")),
        duration_ms=int(float(duration) * 1000.0) if isinstance(duration, int | float) else 0,
        usage=parse_usage(payload.get("usage")),
        denied_actions=denied,
    )


def _transcript_turn_recovered(conversation_id: str | None, *, home: Path | None = None) -> bool:
    if not conversation_id:
        return False
    rows = _read_transcript_rows(conversation_id, home=home)
    if not rows:
        return False
    for row in reversed(rows):
        step_type = _text(row.get("type"))
        step_status = _text(row.get("status"))
        if step_type == "ERROR_MESSAGE" or step_status in {"ERROR", "FAILED", "CANCELED"}:
            return False
        if step_type == "PLANNER_RESPONSE" and step_status == "DONE":
            return True
        if step_type == "USER_INPUT":
            break
    return False


def _denial_label(entry: object) -> str:
    item = _mapping(entry)
    action = _text(item.get("action")) or "unknown"
    display = _text(item.get("display_name")) or ""
    return f"{action}:{display}".rstrip(":")


def _tool_failed(info: Mapping[str, object]) -> bool:
    # meiva:note
    # `agy` does not mark a failing shell command: a non-zero exit arrives as
    # plain output text. Only a tool-level error field is trustworthy here.
    return bool(info.get("error") or info.get("Error"))


def _tool_content(info: Mapping[str, object]) -> str:
    for key in ("output", "Output", "combinedOutput", "combined_output"):
        value = info.get(key)
        if isinstance(value, str):
            return value
        if value is not None:
            return json.dumps(value, ensure_ascii=False)
    return ""


def _read_transcript_rows(
    conversation_id: str, *, home: Path | None = None
) -> list[dict[str, object]]:
    log_dir = (
        resolve_user_home(home)
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
    )
    for filename in ("transcript_full.jsonl", "transcript.jsonl"):
        path = log_dir / filename
        if not path.is_file():
            continue
        try:
            rows: list[dict[str, object]] = []
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                parsed = object_of(json.loads, line)
                if is_object_dict(parsed):
                    rows.append(parsed)
            return rows
        except (OSError, json.JSONDecodeError):
            continue
    return []


def _transcript_tool_args(conversation_id: str, step_index: int, name: str) -> dict[str, object]:
    rows = _read_transcript_rows(conversation_id)
    for row in reversed(rows):
        idx = _int(row.get("step_index"))
        if step_index > 0 and idx > step_index:
            continue
        raw_calls = row.get("tool_calls")
        calls = raw_calls if is_object_list(raw_calls) else []
        for raw_tc in calls:
            tc = _mapping(raw_tc)
            if _text(tc.get("name")) == name:
                return dict(_mapping(tc.get("args")))
    return {}


def _transcript_tool_output(conversation_id: str, step_index: int) -> str:
    for row in _read_transcript_rows(conversation_id):
        if _int(row.get("step_index")) == step_index:
            content = _text(row.get("content"))
            if content:
                return content
    return ""


def _transcript_thinking(conversation_id: str, step_index: int, *, before_tool: bool) -> str | None:
    target_idx = step_index - 1 if before_tool else step_index
    if target_idx < 0:
        return None
    for row in _read_transcript_rows(conversation_id):
        if _int(row.get("step_index")) != target_idx:
            continue
        if _text(row.get("type")) != "PLANNER_RESPONSE":
            continue
        if not before_tool and row.get("tool_calls"):
            return None
        thinking = _text(row.get("thinking"))
        if thinking and thinking.strip():
            return thinking.strip()
    return None


def _mapping(value: object) -> dict[str, object]:
    return value if is_object_dict(value) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int:
    return int(value) if isinstance(value, int | float) else 0
