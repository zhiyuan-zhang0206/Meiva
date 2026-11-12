from __future__ import annotations

from collections.abc import Mapping

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.session.jsonl.store_shapes import INPUT_SIZE_KEY, bounded_input, image_stub
from base.lm.harness.session.jsonl.store_text import (
    MAIN_TEXT_CAP_BYTES,
    SCALAR_CAP_BYTES,
    SUBAGENT_TEXT_CAP_BYTES,
    cut,
    mark_truncated,
    scalar,
    scalars,
    scrub,
    text_field,
)

# meiva:note
# What of a Claude transcript row goes into our database: a whitelist, not a blacklist. Only the row types and
# fields the parser reads are kept, in the shapes the parser reads them (filtered raw rows, so a corrected
# parser can reproduce the timeline from the database alone). Everything else is dropped: the per-request
# bookkeeping (`api-request*`, `queue-operation`, `file-history-snapshot`, `session-owner`, `cost-state`, titles
# and mode markers), the context attachments, and any row carrying an account identifier or a request body. A
# row type this module does not know is not stored; the parser yields one anomaly entry for each such row (it
# does not de-duplicate) and the projector reports each type once. `error` is kept only as the string the parser reads and `origin` only as
# its `kind`, so no other key they might carry is stored. How text is cut and cleaned (the caps, `scrub`) is
# `session/jsonl/store_text.py`.
# meiva:link base/lm/harness/docs/transcript-projection.md

__all__ = [
    "INPUT_SIZE_KEY",
    "MAIN_TEXT_CAP_BYTES",
    "SUBAGENT_TEXT_CAP_BYTES",
    "scrub",
    "stored_row",
]


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


def stored_row(raw: Mapping[str, object], *, subagent: bool) -> dict[str, object] | None:
    cap = SUBAGENT_TEXT_CAP_BYTES if subagent else MAIN_TEXT_CAP_BYTES
    kept = _kept(raw, cap)
    if kept is not None:
        _ = scrub(kept)
    return kept


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
        queued = scalars(attachment, _QUEUED_FIELDS)
        origin = attachment.get("origin")
        kind = origin.get("kind") if is_object_dict(origin) else None
        if isinstance(kind, str):
            queued["origin"] = {"kind": cut(kind, SCALAR_CAP_BYTES)}
        prompt = attachment.get("prompt")
        if prompt is not None:
            queued["prompt"] = cut(prompt, cap) if isinstance(prompt, str) else None
            mark_truncated(queued, "prompt", prompt)
        return {**_header(raw), "attachment": queued}
    return None


def _header(raw: Mapping[str, object]) -> dict[str, object]:
    kept = scalars(raw, _SCALAR_FIELDS)
    error = raw.get("error")
    if isinstance(error, str):
        kept["error"] = cut(error, SCALAR_CAP_BYTES)
    origin = raw.get("origin")
    kind = origin.get("kind") if is_object_dict(origin) else None
    if isinstance(kind, str):
        kept["origin"] = {"kind": cut(kind, SCALAR_CAP_BYTES)}
    return kept


def _conversation(raw: Mapping[str, object], cap: int) -> dict[str, object]:
    kept = _header(raw)
    result = raw.get("toolUseResult")
    if is_object_dict(result):
        kept["toolUseResult"] = scalars(result, _TOOL_RESULT_FIELDS)
    message = raw.get("message")
    if is_object_dict(message):
        kept["message"] = _message(message, cap)
    return kept


def _message(message: Mapping[str, object], cap: int) -> dict[str, object]:
    kept = scalars(message, _MESSAGE_FIELDS)
    usage = message.get("usage")
    if is_object_dict(usage):
        kept["usage"] = {
            field: usage[field]
            for field in _USAGE_FIELDS
            if isinstance(usage.get(field), int | float) and not isinstance(usage.get(field), bool)
        }
    content = message.get("content")
    if isinstance(content, str):
        kept["content"] = cut(content, cap)
        mark_truncated(kept, "content", content)
    elif is_object_list(content):
        kept["content"] = [_block(block, cap, nested=False) for block in content]
    return kept


def _block(block: object, cap: int, *, nested: bool) -> object:
    if not is_object_dict(block):
        return None
    kind = block.get("type")
    kind = cut(kind, SCALAR_CAP_BYTES) if isinstance(kind, str) else None
    if kind == "image":
        return _image(block)
    if kind == "text":
        return text_field({"type": "text", "text": block.get("text")}, "text", cap)
    if nested:
        return {"type": kind}
    if kind == "thinking":
        return text_field({"type": "thinking", "thinking": block.get("thinking")}, "thinking", cap)
    if kind == "tool_use":
        name = block.get("name")
        return {
            "type": "tool_use",
            "id": scalar(block.get("id")),
            "name": cut(name, SCALAR_CAP_BYTES) if isinstance(name, str) else None,
            "input": bounded_input(block.get("input"), cap),
        }
    if kind == "tool_result":
        content = block.get("content")
        fields: dict[str, object] = {
            "type": "tool_result",
            "tool_use_id": scalar(block.get("tool_use_id")),
            "is_error": scalar(block.get("is_error")),
            "content": _result_content(content, cap),
        }
        return text_field(fields, "content", cap, original=content)
    return {"type": kind}


def _result_content(content: object, cap: int) -> object:
    if isinstance(content, str):
        return cut(content, cap)
    if is_object_list(content):
        return [_block(block, cap, nested=True) for block in content]
    return None


def _image(block: Mapping[str, object]) -> dict[str, object]:
    source = block.get("source")
    data = source.get("data") if is_object_dict(source) else None
    media = source.get("media_type") if is_object_dict(source) else None
    raw = data.encode(errors="replace") if isinstance(data, str) else b""
    return image_stub(raw, media if isinstance(media, str) else None)
