from __future__ import annotations

from collections.abc import Callable, Mapping

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.session.jsonl.store_shapes import bounded_input, image_stub
from base.lm.harness.session.jsonl.store_text import (
    MAIN_TEXT_CAP_BYTES,
    SCALAR_CAP_BYTES,
    SUBAGENT_TEXT_CAP_BYTES,
    cut,
    scalar,
    scalars,
    text_field,
)

# meiva:note
# What of Codex's own record goes into our database: a whitelist, not a blacklist, in the shapes the parser reads.
# Two kinds of row are stored. A history row is one entry of `thread/items/list` (`{turnId, item, startedAtMs,
# completedAtMs}`), kept per item type with only the fields `tools.py` and `items.py` read; an item type the
# parser does not turn into entries (the quiet ones, and any type it does not know) is not stored. A rollout row is
# one of the few lines the reader asks the rollout file for: a turn's end or abort, a sample's token usage and the
# sub-agent report a finished child leaves in its parent, reduced to the fields `rollout.py` reads. Image bytes are
# never stored (an inline `data:` URL becomes `{sha256, bytes, media_type}`; a link or a file id stays a short
# reference); a text field is cut at the record's cap with its original length (`session/jsonl/store_text.py`: 256
# KiB for the main record, 16 KiB for a sub-agent's) and a tool's arguments are bounded as a whole.
# meiva:link base/lm/harness/docs/transcript-projection.md

_ITEM_SCALARS = ("type", "id", "startedAtMs", "completedAtMs")
_ROW_SCALARS = ("turnId", "startedAtMs", "completedAtMs")
_LIST_KEPT = 64
_COUNTS = ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens")


def stored_item_row(raw: Mapping[str, object], *, subagent: bool) -> dict[str, object] | None:
    item = raw.get("item")
    if not is_object_dict(item):
        return None
    cap = SUBAGENT_TEXT_CAP_BYTES if subagent else MAIN_TEXT_CAP_BYTES
    kept = _item(item, cap)
    if kept is None:
        return None
    return {**scalars(raw, _ROW_SCALARS), "item": kept}


def stored_rollout_line(raw: Mapping[str, object]) -> dict[str, object] | None:
    payload = raw.get("payload")
    if not is_object_dict(payload):
        return None
    kind, inner = raw.get("type"), payload.get("type")
    if kind == "event_msg" and inner in ("task_complete", "turn_aborted"):
        body = scalars(payload, ("type", "turn_id", "reason"))
        error = payload.get("error")
        if is_object_dict(error):
            body["error"] = scalars(error, ("message", "codex_error_info"))
        return _line(raw, body)
    if kind == "token_usage_record":
        usage = payload.get("usage")
        counts = (
            {name: usage[name] for name in _COUNTS if _is_count(usage.get(name))}
            if is_object_dict(usage)
            else {}
        )
        return _line(raw, {**scalars(payload, ("response_id", "turn_id")), "usage": counts})
    if kind == "response_item" and inner == "message" and payload.get("role") == "user":
        return _report(raw, payload)
    return None


def _line(raw: Mapping[str, object], payload: dict[str, object]) -> dict[str, object]:
    return {**scalars(raw, ("timestamp", "ordinal", "type")), "payload": payload}


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _report(raw: Mapping[str, object], payload: Mapping[str, object]) -> dict[str, object] | None:
    content = payload.get("content")
    if not is_object_list(content):
        return None
    blocks = [
        {"text": cut(text, SCALAR_CAP_BYTES)}
        for block in content
        if is_object_dict(block) and isinstance(text := block.get("text"), str)
    ]
    passthrough = payload.get("internal_chat_message_metadata_passthrough")
    kept: dict[str, object] = {
        **scalars(payload, ("type", "id", "role")),
        "content": blocks,
        "internal_chat_message_metadata_passthrough": scalars(
            passthrough if is_object_dict(passthrough) else {}, ("turn_id",)
        ),
    }
    return _line(raw, kept)


def _item(item: Mapping[str, object], cap: int) -> dict[str, object] | None:
    kind = item.get("type")
    build = _BUILDERS.get(kind) if isinstance(kind, str) else None
    if build is None:
        return None
    return {**scalars(item, _ITEM_SCALARS), **build(item, cap)}


def _text(item: Mapping[str, object], field: str, cap: int) -> dict[str, object]:
    return text_field({field: item.get(field)}, field, cap)


def _texts(value: object, cap: int) -> list[str]:
    if not is_object_list(value):
        return []
    return [cut(element, cap) for element in value[:_LIST_KEPT] if isinstance(element, str)]


def _user(item: Mapping[str, object], cap: int) -> dict[str, object]:
    content = item.get("content")
    parts = [_part(part, cap) for part in (content if is_object_list(content) else [])]
    return {**scalars(item, ("clientId",)), "content": parts}


def _part(part: object, cap: int) -> dict[str, object]:
    if not is_object_dict(part):
        return {"type": None}
    kind = part.get("type")
    if kind == "text":
        return text_field({"type": "text", "text": part.get("text")}, "text", cap)
    if kind == "image":
        return _image(part)
    return {"type": kind if isinstance(kind, str) else None, **scalars(part, ("path", "name"))}


# meiva:note
# An inline image is a `data:<media type>;base64,<bytes>` URL: its bytes are replaced by their hash and size; any
# other URL (or a file id) stays as the short reference it is.
def _image(part: Mapping[str, object]) -> dict[str, object]:
    url = part.get("url")
    if isinstance(url, str) and url.startswith("data:"):
        head = url.partition(",")[0]
        media = head.removeprefix("data:").partition(";")[0] or None
        return image_stub(url.encode(errors="replace"), media)
    return {"type": "image", **scalars(part, ("url", "fileId", "detail"))}


def _agent(item: Mapping[str, object], cap: int) -> dict[str, object]:
    return {**scalars(item, ("phase",)), **_text(item, "text", cap)}


def _reasoning(item: Mapping[str, object], cap: int) -> dict[str, object]:
    return {
        "summary": _texts(item.get("summary"), cap),
        "content": _texts(item.get("content"), cap),
    }


def _command(item: Mapping[str, object], cap: int) -> dict[str, object]:
    kept = scalars(item, ("cwd", "status", "exitCode", "durationMs"))
    command = text_field({"command": item.get("command")}, "command", cap)
    output = text_field({"aggregatedOutput": item.get("aggregatedOutput")}, "aggregatedOutput", cap)
    return {**kept, **command, **output}


def _collab(item: Mapping[str, object], cap: int) -> dict[str, object]:
    kept = scalars(item, ("tool", "status", "model"))
    kept["receiverThreadIds"] = _texts(item.get("receiverThreadIds"), SCALAR_CAP_BYTES)
    agents = item.get("receiverAgents")
    kept["receiverAgents"] = [
        scalars(agent, ("threadId", "agentNickname"))
        for agent in (agents[:_LIST_KEPT] if is_object_list(agents) else [])
        if is_object_dict(agent)
    ]
    states = item.get("agentsStates")
    kept["agentsStates"] = {
        key: _state(state, cap)
        for key, state in (list(states.items())[:_LIST_KEPT] if is_object_dict(states) else [])
        if is_object_dict(state)
    }
    return {**kept, **_text(item, "prompt", cap)}


def _state(state: Mapping[str, object], cap: int) -> dict[str, object]:
    message = state.get("message")
    return {
        **scalars(state, ("status",)),
        "message": cut(message, cap) if isinstance(message, str) else None,
    }


def _file_change(item: Mapping[str, object], cap: int) -> dict[str, object]:
    changes = item.get("changes")
    listed = [
        change for change in (changes if is_object_list(changes) else []) if is_object_dict(change)
    ]
    listed = listed[:_LIST_KEPT]
    each = max(cap // max(len(listed), 1), SCALAR_CAP_BYTES)
    kept: list[dict[str, object]] = []
    for change in listed:
        kind = change.get("kind")
        entry = {**scalars(change, ("path",)), **_text(change, "diff", each)}
        entry["kind"] = (
            scalars(kind, ("type", "move_path")) if is_object_dict(kind) else scalar(kind)
        )
        kept.append(entry)
    return {**scalars(item, ("status",)), "changes": kept}


def _arguments(value: object, cap: int) -> object:
    return bounded_input(value, cap) if is_object_dict(value) else scalar(value)


def _blocks(value: object, field: str, cap: int) -> list[dict[str, object]]:
    return [
        text_field({"type": block.get("type"), field: block.get(field)}, field, cap)
        for block in (value[:_LIST_KEPT] if is_object_list(value) else [])
        if is_object_dict(block) and isinstance(block.get(field), str)
    ]


def _mcp(item: Mapping[str, object], cap: int) -> dict[str, object]:
    kept = scalars(item, ("server", "tool", "status", "durationMs"))
    kept["arguments"] = _arguments(item.get("arguments"), cap)
    result = item.get("result")
    if is_object_dict(result):
        kept["result"] = {"content": _blocks(result.get("content"), "text", cap)}
    error = item.get("error")
    if is_object_dict(error):
        kept["error"] = _text(error, "message", cap)
    return kept


def _dynamic(item: Mapping[str, object], cap: int) -> dict[str, object]:
    kept = scalars(item, ("tool", "status", "success", "durationMs"))
    kept["arguments"] = _arguments(item.get("arguments"), cap)
    kept["contentItems"] = _blocks(item.get("contentItems"), "text", cap)
    return kept


def _web_search(item: Mapping[str, object], cap: int) -> dict[str, object]:
    kept = _text(item, "query", cap)
    action = item.get("action")
    if is_object_dict(action):
        shallow = scalars(action, ("type", "query", "url", "pattern"))
        shallow["queries"] = _texts(action.get("queries"), SCALAR_CAP_BYTES)
        kept["action"] = shallow
    results = item.get("results")
    if is_object_list(results):
        kept["results"] = bounded_input({"results": results}, cap)
    return kept


def _path(item: Mapping[str, object], _cap: int) -> dict[str, object]:
    return scalars(item, ("path",))


def _sleep(item: Mapping[str, object], _cap: int) -> dict[str, object]:
    return scalars(item, ("durationMs",))


def _image_generation(item: Mapping[str, object], cap: int) -> dict[str, object]:
    kept = scalars(item, ("status", "savedPath"))
    kept.update(_text(item, "revisedPrompt", cap))
    failure = item.get("failure")
    if failure is not None:
        kept["failure"] = (
            bounded_input(failure, cap) if is_object_dict(failure) else scalar(failure)
        )
    return kept


def _marker(_item: Mapping[str, object], _cap: int) -> dict[str, object]:
    return {}


def _plan(item: Mapping[str, object], cap: int) -> dict[str, object]:
    return _text(item, "text", cap)


def _review(item: Mapping[str, object], cap: int) -> dict[str, object]:
    return _text(item, "review", cap)


_BUILDERS: dict[str, Callable[[Mapping[str, object], int], dict[str, object]]] = {
    "userMessage": _user,
    "agentMessage": _agent,
    "reasoning": _reasoning,
    "plan": _plan,
    "contextCompaction": _marker,
    "commandExecution": _command,
    "collabAgentToolCall": _collab,
    "fileChange": _file_change,
    "mcpToolCall": _mcp,
    "dynamicToolCall": _dynamic,
    "webSearch": _web_search,
    "imageView": _path,
    "sleep": _sleep,
    "imageGeneration": _image_generation,
    "enteredReviewMode": _review,
    "exitedReviewMode": _review,
}
