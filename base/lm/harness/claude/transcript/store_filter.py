from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from base.host.boundary.json_values import is_object_dict, is_object_list

# meiva:note
# What of a Claude transcript row goes into our database: a whitelist, not a blacklist. Only the row types and
# fields the parser reads are kept, in the shapes the parser reads them (filtered raw rows, so a corrected
# parser can reproduce the timeline from the database alone). Everything else is dropped: the per-request
# bookkeeping (`api-request*`, `queue-operation`, `file-history-snapshot`, `session-owner`, `cost-state`, titles
# and mode markers), the context attachments, and any row carrying an account identifier or a request body. A
# row type this module does not know is not stored; the parser yields one anomaly entry for each such row (it
# does not de-duplicate) and the projector reports each type once. `error` is kept only as the string the parser reads and `origin` only as
# its `kind`, so no other key they might carry is stored.
#
# Size, per text field (a text block, a thinking block, a tool result, a queued prompt, a string value of a
# tool input): a main record's field is cut at `MAIN_TEXT_CAP_BYTES`, 256 KiB, which keeps today's behaviour
# that the main agent's own output is not truncated on the timeline; a sub-agent record's field is cut at
# `SUBAGENT_TEXT_CAP_BYTES`, 16 KiB, about twice the 8,000 characters the timeline shows of a sub-agent's item
# (`SUBAGENT_ITEM_CHARS` in `agent/graph/claim/harness_activity.py`; `base` cannot import it), because a
# sub-agent's tool output is the bulk of a long session. A cut field keeps its head and gains
# `truncated_from_bytes`, the original length. A tool input is bounded as a whole: over the same cap it is
# replaced by a shallow summary (its scalar and short values, every long string cut, nested structures that
# are not short replaced by their type and size) carrying `INPUT_SIZE_KEY`, its original size. Image bytes
# become `{sha256, bytes, media_type}`.
#
# Whatever reaches the database must be storable as `jsonb`: `scrub` removes the NUL character and replaces a
# lone surrogate, which `jsonb` refuses and which would otherwise roll back the write of the batch and keep
# the position from moving, so the writer would stay on that row for good. `scrub` is iterative, so nesting
# depth cannot exhaust the stack.
# meiva:link base/lm/harness/docs/transcript-projection.md

MAIN_TEXT_CAP_BYTES = 262_144
SUBAGENT_TEXT_CAP_BYTES = 16_384
SCALAR_CAP_BYTES = 1_024
NESTED_KEEP_BYTES = 2_048
INPUT_SIZE_KEY = "meiva_truncated_from_bytes"
_INPUT_KEYS_KEPT = 32
_KEY_CAP_BYTES = 64

_SCALAR_FIELDS = (
    "type",
    "subtype",
    "uuid",
    "timestamp",
    "version",
    "isSidechain",
    "agentId",
    "isMeta",
    "isCompactSummary",
    "isApiErrorMessage",
    "isAbortedMidStream",
    "promptId",
    "toolDenialKind",
    "thinkingDurationMs",
    "apiErrorStatus",
)
_TOOL_RESULT_FIELDS = ("agentId", "isAsync", "backgroundTaskId", "status")
_MESSAGE_FIELDS = ("id", "model", "stop_reason")
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_QUEUED_FIELDS = ("type", "source_uuid")
_DIRTY = re.compile("[\x00\ud800-\udfff]")


def stored_row(raw: Mapping[str, object], *, subagent: bool) -> dict[str, object] | None:
    cap = SUBAGENT_TEXT_CAP_BYTES if subagent else MAIN_TEXT_CAP_BYTES
    kept = _kept(raw, cap)
    if kept is not None:
        _ = scrub(kept)
    return kept


# meiva:note
# Make a decoded JSON value storable as `jsonb`, in place: the NUL character is removed and a lone surrogate
# becomes U+FFFD, in every string and key. Returns the value, or the cleaned string when given a string. The
# reader runs it on every raw row before the row is parsed, so the timeline items and the stored rows agree.
def scrub(value: object) -> object:
    if isinstance(value, str):
        return _clean(value)
    pending: list[object] = [value]
    while pending:
        node = pending.pop()
        if is_object_dict(node):
            _scrub_dict(node, pending)
        elif is_object_list(node):
            _scrub_list(node, pending)
    return value


def _scrub_dict(node: dict[str, object], pending: list[object]) -> None:
    for key in list(node):
        item = node[key]
        if isinstance(item, str):
            node[key] = _clean(item)
        elif is_object_dict(item) or is_object_list(item):
            pending.append(item)
        if _DIRTY.search(key):
            node[_clean(key)] = node.pop(key)


def _scrub_list(node: list[object], pending: list[object]) -> None:
    for index, item in enumerate(node):
        if isinstance(item, str):
            node[index] = _clean(item)
        elif is_object_dict(item) or is_object_list(item):
            pending.append(item)


def _clean(text: str) -> str:
    if _DIRTY.search(text) is None:
        return text
    return _DIRTY.sub(lambda found: "" if found.group() == "\x00" else "�", text)


def _kept(raw: Mapping[str, object], cap: int) -> dict[str, object] | None:
    row_type = raw.get("type")
    if row_type in ("user", "assistant"):
        return _conversation(raw, cap)
    if row_type == "system" and raw.get("subtype") == "compact_boundary":
        return _header(raw)
    attachment = raw.get("attachment")
    if (
        row_type == "attachment"
        and is_object_dict(attachment)
        and attachment.get("type") == "queued_command"
    ):
        queued = _scalars(attachment, _QUEUED_FIELDS)
        origin = attachment.get("origin")
        kind = origin.get("kind") if is_object_dict(origin) else None
        if isinstance(kind, str):
            queued["origin"] = {"kind": _cut(kind, SCALAR_CAP_BYTES)}
        prompt = attachment.get("prompt")
        if prompt is not None:
            queued["prompt"] = _cut(prompt, cap) if isinstance(prompt, str) else None
            _mark(queued, "prompt", prompt)
        return {**_header(raw), "attachment": queued}
    return None


def _scalars(raw: Mapping[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    kept: dict[str, object] = {}
    for field in fields:
        value = raw.get(field)
        if isinstance(value, str):
            kept[field] = _cut(value, SCALAR_CAP_BYTES)
        elif field in raw and (value is None or isinstance(value, bool | int | float)):
            kept[field] = value
    return kept


def _header(raw: Mapping[str, object]) -> dict[str, object]:
    kept = _scalars(raw, _SCALAR_FIELDS)
    error = raw.get("error")
    if isinstance(error, str):
        kept["error"] = _cut(error, SCALAR_CAP_BYTES)
    origin = raw.get("origin")
    kind = origin.get("kind") if is_object_dict(origin) else None
    if isinstance(kind, str):
        kept["origin"] = {"kind": _cut(kind, SCALAR_CAP_BYTES)}
    return kept


def _conversation(raw: Mapping[str, object], cap: int) -> dict[str, object]:
    kept = _header(raw)
    result = raw.get("toolUseResult")
    if is_object_dict(result):
        kept["toolUseResult"] = _scalars(result, _TOOL_RESULT_FIELDS)
    message = raw.get("message")
    if is_object_dict(message):
        kept["message"] = _message(message, cap)
    return kept


def _message(message: Mapping[str, object], cap: int) -> dict[str, object]:
    kept = _scalars(message, _MESSAGE_FIELDS)
    usage = message.get("usage")
    if is_object_dict(usage):
        kept["usage"] = {
            field: usage[field]
            for field in _USAGE_FIELDS
            if isinstance(usage.get(field), int | float) and not isinstance(usage.get(field), bool)
        }
    content = message.get("content")
    if isinstance(content, str):
        kept["content"] = _cut(content, cap)
        _mark(kept, "content", content)
    elif is_object_list(content):
        kept["content"] = [_block(block, cap, nested=False) for block in content]
    return kept


def _block(block: object, cap: int, *, nested: bool) -> object:
    if not is_object_dict(block):
        return None
    kind = block.get("type")
    kind = _cut(kind, SCALAR_CAP_BYTES) if isinstance(kind, str) else None
    if kind == "image":
        return _image(block)
    if kind == "text":
        return _text_field({"type": "text", "text": block.get("text")}, "text", cap)
    if nested:
        return {"type": kind}
    if kind == "thinking":
        return _text_field({"type": "thinking", "thinking": block.get("thinking")}, "thinking", cap)
    if kind == "tool_use":
        name = block.get("name")
        return {
            "type": "tool_use",
            "id": _scalar(block.get("id")),
            "name": _cut(name, SCALAR_CAP_BYTES) if isinstance(name, str) else None,
            "input": _bounded_input(block.get("input"), cap),
        }
    if kind == "tool_result":
        content = block.get("content")
        fields: dict[str, object] = {
            "type": "tool_result",
            "tool_use_id": _scalar(block.get("tool_use_id")),
            "is_error": _scalar(block.get("is_error")),
            "content": _result_content(content, cap),
        }
        return _text_field(fields, "content", cap, original=content)
    return {"type": kind}


def _scalar(value: object) -> object:
    if isinstance(value, str):
        return _cut(value, SCALAR_CAP_BYTES)
    return value if value is None or isinstance(value, bool | int | float) else None


def _result_content(content: object, cap: int) -> object:
    if isinstance(content, str):
        return _cut(content, cap)
    if is_object_list(content):
        return [_block(block, cap, nested=True) for block in content]
    return None


def _image(block: Mapping[str, object]) -> dict[str, object]:
    source = block.get("source")
    data = source.get("data") if is_object_dict(source) else None
    media = source.get("media_type") if is_object_dict(source) else None
    raw = data.encode(errors="replace") if isinstance(data, str) else b""
    return {
        "type": "image",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "media_type": _cut(media, SCALAR_CAP_BYTES) if isinstance(media, str) else None,
    }


def _size(value: str) -> int:
    return len(value.encode(errors="replace"))


def _cut(value: str, cap: int) -> str:
    if len(value) <= cap // 4 or _size(value) <= cap:
        return value
    return value.encode(errors="replace")[:cap].decode(errors="ignore")


def _mark(fields: dict[str, object], name: str, original: object) -> None:
    kept = fields.get(name)
    if isinstance(original, str) and isinstance(kept, str) and kept != original:
        fields["truncated_from_bytes"] = _size(original)


def _text_field(
    fields: dict[str, object], name: str, cap: int, *, original: object | None = None
) -> dict[str, object]:
    source = fields.get(name) if original is None else original
    if isinstance(source, str):
        fields[name] = _cut(source, cap)
        _mark(fields, name, source)
    elif original is None:
        fields[name] = None
    return fields


def _json_size(value: object) -> int | None:
    try:
        return len(json.dumps(value, ensure_ascii=False).encode(errors="replace"))
    except (RecursionError, ValueError, TypeError):
        return None


def _bounded_input(value: object, cap: int) -> object:
    if not is_object_dict(value):
        return None
    size = _json_size(value)
    if size is not None and size <= cap:
        return value
    summary = _summary(value, cap)
    summary[INPUT_SIZE_KEY] = size
    if (summarized := _json_size(summary)) is not None and summarized <= cap:
        return summary
    return {
        "keys": [_cut(key, _KEY_CAP_BYTES) for key in list(value)[:_INPUT_KEYS_KEPT]],
        INPUT_SIZE_KEY: size,
    }


def _summary(value: Mapping[str, object], cap: int) -> dict[str, object]:
    strings = sum(1 for item in value.values() if isinstance(item, str))
    budget = max(cap // max(strings, 1), SCALAR_CAP_BYTES)
    return {key: _summarized(item, budget) for key, item in value.items()}


def _summarized(item: object, budget: int) -> object:
    if isinstance(item, str):
        return _cut(item, budget)
    if item is None or isinstance(item, bool | int | float):
        return item
    nested = _json_size(item)
    if nested is not None and nested <= NESTED_KEEP_BYTES:
        return item
    return {"type": type(item).__name__, "bytes": nested}
