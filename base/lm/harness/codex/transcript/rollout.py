from __future__ import annotations

import shlex
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.codex.transcript.clock import iso_at, parse_iso_ms
from base.lm.harness.codex.transcript.errors import MissingFieldError, UnknownValueError
from base.lm.harness.codex.transcript.items import (
    Where,
    as_anomaly,
    interrupt_entry,
    item_entries,
    remember_inputs,
    turn_end_entry,
)
from base.lm.harness.codex.transcript.scalars import opt_int, opt_text, req_int, text
from base.lm.harness.codex.transcript.shapes import Obj, obj, objects, opt_objects, strings
from base.lm.harness.events import HarnessCause, Usage
from base.lm.harness.session.transcript.entries import (
    AnyEntry,
    Notice,
    Order,
    TokenUsage,
)

# meiva:note
# The rollout read as a supplement to the protocol history, for what the history lacks: per-sample
# token usage (`token_usage_record`), the turn's own end (`task_complete`, `turn_aborted`) and the
# sub-agent report a finished child leaves in its parent (`<subagent_notification>`). Content the
# history also carries is read too, so a rollout alone is enough, and the double write is resolved by
# key: an `event_msg/item_completed` and the `response_item` of the same assistant message or reasoning
# share the item id and so the key, and the projection keeps the first. Three rows are deliberately not
# read: a user `response_item` (its twin in `item_completed` is the only one carrying the client id, and
# its own id differs), the tool-call and output `response_item`s (a command is structured only in
# `item_completed`; the call is the orchestrator's JavaScript) and the `<turn_aborted>` message (the
# `turn_aborted` event says the same).
# meiva:link base/lm/harness/docs/transcript-projection.md

_QUIET_LINES = frozenset(
    {
        "session_meta",
        "turn_context",
        "world_state",
        "compacted",
        "inter_agent_communication_metadata",
    }
)
_QUIET_EVENTS = frozenset({"task_started", "token_count", "thread_settings_applied"})
_QUIET_RESPONSES = frozenset(
    {
        "reasoning",
        "agent_message",
        "configuration_update",
        "additional_tools",
        "custom_tool_call",
        "custom_tool_call_output",
        "function_call",
        "function_call_output",
    }
)
_NOTIFICATION = "<subagent_notification>"
_NOTIFICATION_END = "</subagent_notification>"


def parse_rollout(
    lines: Iterable[object], *, record: str, input_turns: Collection[str] = ()
) -> list[AnyEntry]:
    opened = set(input_turns)
    entries: list[AnyEntry] = []
    for line in lines:
        read = parse_rollout_line(line, record=record, input_turns=opened)
        remember_inputs(read, opened)
        entries.extend(read)
    return entries


def parse_rollout_line(
    line: object, *, record: str, input_turns: Collection[str] = ()
) -> list[AnyEntry]:
    row: Obj = line if is_object_dict(line) else {}
    subject = f"line{opt_int(row, 'ordinal')}"
    try:
        kind = text(row, "type")
        if kind in _QUIET_LINES:
            return []
        reader = _reader(kind)
        at = Stamp(parse_iso_ms(text(row, "timestamp"), "timestamp"), req_int(row, "ordinal"))
        return reader(obj(row.get("payload"), "payload"), at, record, input_turns)
    except (MissingFieldError, UnknownValueError) as error:
        order = Order(opt_int(row, "ordinal") or 0, 0)
        return [as_anomaly(error, subject=subject, order=order, record=record, turn=None)]


def _reader(kind: str) -> LineReader:
    reader = _LINES.get(kind)
    if reader is None:
        raise UnknownValueError(kind)
    return reader


@dataclass(frozen=True, slots=True)
class Stamp:
    ms: int
    seq: int


type LineReader = Callable[[Obj, Stamp, str, Collection[str]], list[AnyEntry]]


def _event(payload: Obj, at: Stamp, record: str, input_turns: Collection[str]) -> list[AnyEntry]:
    kind = text(payload, "type")
    if kind in _QUIET_EVENTS:
        return []
    if kind == "item_completed":
        return _item_completed(payload, at, record, input_turns)
    if kind == "task_complete":
        return [turn_end_entry(text(payload, "turn_id"), at.ms, at.seq, record)]
    if kind == "turn_aborted":
        return [interrupt_entry(text(payload, "turn_id"), at.ms, at.seq, record)]
    raise UnknownValueError(f"event_msg/{kind}")


def _item_completed(
    payload: Obj, at: Stamp, record: str, input_turns: Collection[str]
) -> list[AnyEntry]:
    item = obj(payload.get("item"), "item")
    where = Where(
        turn=text(payload, "turn_id"),
        record=record,
        started_ms=req_int(payload, "started_at_ms"),
        completed_ms=opt_int(payload, "completed_at_ms"),
        seq=at.seq,
    )
    return item_entries(_as_history_item(item), where, input_turns)


def _response(
    payload: Obj, at: Stamp, record: str, _input_turns: Collection[str]
) -> list[AnyEntry]:
    kind = text(payload, "type")
    if kind in _QUIET_RESPONSES:
        return []
    if kind == "message":
        return _message(payload, at, record)
    raise UnknownValueError(f"response_item/{kind}")


def _message(payload: Obj, at: Stamp, record: str) -> list[AnyEntry]:
    role = text(payload, "role")
    if role != "user":
        return []
    body = "".join(text(part, "text") for part in opt_objects(payload, "content") if "text" in part)
    if role == "user" and body.lstrip().startswith(_NOTIFICATION):
        report = body.strip().removeprefix(_NOTIFICATION).removesuffix(_NOTIFICATION_END).strip()
        return [
            Notice(
                key=f"notice:{text(payload, 'id')}",
                order=Order(at.seq, 0),
                record=record,
                at=iso_at(at.ms),
                text=report,
                cause=HarnessCause.BACKGROUND_TASK,
                opens_turn=False,
            )
        ]
    return []


def _usage(payload: Obj, at: Stamp, record: str, _input_turns: Collection[str]) -> list[AnyEntry]:
    sample = obj(payload.get("usage"), "usage")
    response = text(payload, "response_id")
    return [
        TokenUsage(
            key=f"usage:{response}",
            order=Order(at.seq, 0),
            record=record,
            turn=text(payload, "turn_id"),
            at=iso_at(at.ms),
            message_ref=response,
            usage=Usage(
                input_tokens=req_int(sample, "input_tokens"),
                output_tokens=req_int(sample, "output_tokens"),
                cache_creation_input_tokens=opt_int(sample, "cache_write_input_tokens") or 0,
                cache_read_input_tokens=opt_int(sample, "cached_input_tokens") or 0,
            ),
        )
    ]


_LINES: dict[str, LineReader] = {
    "event_msg": _event,
    "response_item": _response,
    "token_usage_record": _usage,
}


# meiva:note
# The rollout spells an item in its own Rust-serde shape; this returns the protocol's. The command is
# the argv joined the way a shell would read it back, so the text differs from the history's only in
# quoting.
def _as_history_item(item: Obj) -> Obj:
    kind = text(item, "type")
    convert = _CONVERT.get(kind)
    if convert is None:
        raise UnknownValueError(f"item_completed/{kind}")
    return convert(item)


def _user_item(item: Obj) -> Obj:
    content: list[Obj] = []
    for part in objects(item, "content"):
        if opt_text(part, "type") == "image":
            content.append({"type": "image", "url": part.get("image_url")})
        else:
            content.append(part)
    return {
        "type": "userMessage",
        "id": text(item, "id"),
        "clientId": item.get("client_id"),
        "content": content,
    }


def _agent_item(item: Obj) -> Obj:
    body = "".join(text(part, "text") for part in objects(item, "content"))
    return {
        "type": "agentMessage",
        "id": text(item, "id"),
        "text": body,
        "phase": item.get("phase"),
    }


def _reasoning_item(item: Obj) -> Obj:
    return {
        "type": "reasoning",
        "id": text(item, "id"),
        "summary": item.get("summary_text"),
        "content": item.get("raw_content"),
    }


def _compaction_item(item: Obj) -> Obj:
    return {"type": "contextCompaction", "id": text(item, "id")}


def _command_item(item: Obj) -> Obj:
    spent = obj(item.get("duration"), "duration")
    return {
        "type": "commandExecution",
        "id": text(item, "id"),
        "command": shlex.join(strings(item, "command")),
        "cwd": unquote(urlparse(text(item, "cwd")).path),
        "status": text(item, "status"),
        "aggregatedOutput": item.get("aggregated_output"),
        "exitCode": item.get("exit_code"),
        "durationMs": req_int(spent, "secs") * 1000 + req_int(spent, "nanos") // 1_000_000,
    }


def _collab_item(item: Obj) -> Obj:
    agents: list[Obj] = [
        {"threadId": agent.get("thread_id"), "agentNickname": agent.get("agent_nickname")}
        for agent in opt_objects(item, "receiver_agents")
    ]
    words = text(item, "tool").split("_")
    return {
        "type": "collabAgentToolCall",
        "id": text(item, "id"),
        "tool": words[0] + "".join(word.capitalize() for word in words[1:]),
        "status": text(item, "status"),
        "receiverThreadIds": item.get("receiver_thread_ids"),
        "receiverAgents": agents,
        "prompt": item.get("prompt"),
        "model": item.get("model"),
    }


# meiva:note
# The rollout's file change is a map from path to `{type, content | unified_diff}`; the protocol's is a list of
# `{path, kind, diff}`. A picture is never copied: the image view names the file only.
def _file_change_item(item: Obj) -> Obj:
    changes: list[Obj] = []
    raw = item.get("changes")
    for path, change in (raw if is_object_dict(raw) else {}).items():
        if not is_object_dict(change):
            continue
        diff = change.get("unified_diff") or change.get("content")
        changes.append(
            {
                "path": path,
                "kind": change.get("type"),
                "diff": diff if isinstance(diff, str) else None,
            }
        )
    return {
        "type": "fileChange",
        "id": text(item, "id"),
        "changes": changes,
        "status": text(item, "status"),
        "output": item.get("stdout"),
    }


def _image_view_item(item: Obj) -> Obj:
    return {
        "type": "imageView",
        "id": text(item, "id"),
        "path": unquote(urlparse(text(item, "path")).path),
    }


def _extension_item(item: Obj) -> Obj:
    kind = text(item, "kind")
    if kind == "clock.sleep":
        return {"type": "sleep", "id": text(item, "id"), "durationMs": item.get("durationMs")}
    if kind != "web.search":
        raise UnknownValueError(f"item_completed/Extension:{kind}")
    return {
        "type": "webSearch",
        "id": text(item, "id"),
        "query": text(item, "query"),
        "results": item.get("results"),
    }


# meiva:note
# The rollout's sub-agent activity and MCP call are spelled as the protocol's, except for the camel case of a few names
# and the duration (a seconds and nanoseconds pair, not read).
def _sub_agent_item(item: Obj) -> Obj:
    return {
        "type": "subAgentActivity",
        "id": text(item, "id"),
        "kind": item.get("kind"),
        "agentPath": item.get("agent_path"),
        "agentThreadId": item.get("agent_thread_id"),
    }


def _mcp_item(item: Obj) -> Obj:
    return {
        "type": "mcpToolCall",
        "id": text(item, "id"),
        "server": item.get("server"),
        "tool": item.get("tool"),
        "arguments": item.get("arguments"),
        "result": item.get("result"),
        "status": item.get("status"),
    }


_CONVERT: dict[str, Callable[[Obj], Obj]] = {
    "SubAgentActivity": _sub_agent_item,
    "McpToolCall": _mcp_item,
    "FileChange": _file_change_item,
    "ImageView": _image_view_item,
    "Extension": _extension_item,
    "UserMessage": _user_item,
    "AgentMessage": _agent_item,
    "Reasoning": _reasoning_item,
    "ContextCompaction": _compaction_item,
    "CommandExecution": _command_item,
    "CollabAgentToolCall": _collab_item,
}
