"""Native tool-call normalization and per-call code replacement.

Keep every invocation and its ID. Structured provider content can contain calls
missing from LangChain's normalized list; recover those without joining code.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TypeGuard

from langchain_core.messages import AIMessage, ToolCall

from base.host.boundary.untyped import object_of


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_object_dict(value) and all(isinstance(k, str) for k in value)


def code_from_args(args: object, *, source: str) -> str:
    """The `code` argument of a tool call, strictly. Raises if `args` is not a
    dict or `code` is present but not a string; returns "" only when `code` is
    absent. The single strict extractor the exec path and the llm-node
    log line share, so both fail loud on a malformed tool call the same way."""
    if not _is_str_dict(args):
        raise TypeError(f"{source}.args must be dict, got {type(args).__name__}")
    code = args.get("code")
    if code is None:
        return ""
    if not isinstance(code, str):
        raise TypeError(f"{source}.args['code'] must be str, got {type(code).__name__}")
    return code


def first_tool_call_code(tool_calls: Sequence[ToolCall]) -> str:
    """The first tool call's `code` argument if it is a non-empty string, else ""
    — the graceful read for before_llm / before_exec hooks that bail when there
    is no code (the strict `code_from_args` is for the exec path that must run
    it). Mirrors the hand-rolled `tool_calls[0]["args"].get("code")` those hooks
    used to duplicate."""
    if not tool_calls:
        return ""
    code = tool_calls[0]["args"].get("code")
    return code if isinstance(code, str) else ""


def _content_call_id(block: dict[str, object]) -> str | None:
    if "id" not in block:
        raise TypeError("content tool_use block has no id")
    call_id = block["id"]
    if call_id is not None and not isinstance(call_id, str):
        raise TypeError(f"content tool_use id must be str, got {type(call_id).__name__}")
    return call_id


def _call_from_content(block: dict[str, object]) -> ToolCall:
    call_id = _content_call_id(block)
    name = block.get("name")
    if not isinstance(name, str):
        raise TypeError(f"content tool_use {call_id!r}.name must be str")
    args = block.get("input")
    partial_json = block.get("partial_json")
    if not args and partial_json:
        if not isinstance(partial_json, str):
            raise TypeError(f"content tool_use {call_id!r}.partial_json must be str")
        args = object_of(json.loads, partial_json)
    if not _is_str_dict(args):
        raise TypeError(f"content tool_use {call_id!r}.input must be dict")
    return ToolCall(name=name, args=args, id=call_id, type="tool_call")


def normalize_tool_calls(message: AIMessage) -> AIMessage | None:
    """Recover structured content calls in provider order, preserving each call.

    Return a same-ID replacement only when the native list needs correction.
    Content, arguments, metadata and unrelated invalid calls remain intact.
    """
    native = {call["id"]: call for call in message.tool_calls}
    calls: list[ToolCall] = []
    seen: set[str | None] = set()
    content: object = message.content
    if _is_object_list(content):
        for block in content:
            if not _is_str_dict(block) or block.get("type") != "tool_use":
                continue
            call_id = _content_call_id(block)
            if call_id in seen:
                raise ValueError(f"Duplicate tool_call id: {call_id!r}")
            call = native[call_id] if call_id in native else _call_from_content(block)
            calls.append(call)
            seen.add(call_id)
    calls.extend(call for call in message.tool_calls if call["id"] not in seen)
    ids = [call["id"] for call in calls]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate tool_call ids")
    if calls == message.tool_calls:
        return None
    return message.model_copy(update={"tool_calls": calls})


def replace_execute_code(message: AIMessage, tool_call_id: str | None, code: str) -> AIMessage:
    """Replace one call's code and matching content block, preserving siblings."""
    calls = list(message.tool_calls)
    index = next(i for i, call in enumerate(calls) if call["id"] == tool_call_id)
    call = calls[index]
    calls[index] = {**call, "args": {**call["args"], "code": code}}
    content: object = message.content
    if _is_object_list(content):
        blocks: list[object] = []
        for block in content:
            if (
                _is_str_dict(block)
                and block.get("type") == "tool_use"
                and block["id"] == tool_call_id
            ):
                updated = {**block, "input": calls[index]["args"]}
                if "partial_json" in updated:
                    updated["partial_json"] = json.dumps(calls[index]["args"], ensure_ascii=False)
                blocks.append(updated)
            else:
                blocks.append(block)
        content = blocks
    return message.model_copy(update={"content": content, "tool_calls": calls})
