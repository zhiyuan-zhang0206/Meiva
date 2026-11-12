from __future__ import annotations

import hashlib
from collections.abc import Mapping

from base.host.boundary.json_values import is_object_dict, is_object_list

# meiva:note
# What of a Claude transcript row goes into our database: a whitelist, not a blacklist. Only the row types and
# fields the parser reads are kept, as the CLI wrote them (filtered raw rows, so a corrected parser can
# reproduce the timeline from the database alone). Everything else is dropped: the per-request bookkeeping
# (`api-request*`, `queue-operation`, `file-history-snapshot`, `session-owner`, `cost-state`, titles and mode
# markers), the context attachments, and any row carrying an account identifier or a request body. A type this
# module does not know is not stored (the parser reports it once per type). Large content is not duplicated:
# image bytes become `{sha256, bytes, media_type}` and a text above `ROW_TEXT_CAP_BYTES` is cut and marked with
# its original length.
# meiva:link base/lm/harness/docs/transcript-projection.md

ROW_TEXT_CAP_BYTES = 262_144

_ROW_FIELDS = (
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
    "origin",
    "toolDenialKind",
    "thinkingDurationMs",
    "error",
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
_QUEUED_FIELDS = ("type", "prompt", "source_uuid")


def stored_row(raw: Mapping[str, object]) -> dict[str, object] | None:
    row_type = raw.get("type")
    if row_type in ("user", "assistant"):
        return _conversation(raw)
    if row_type == "system" and raw.get("subtype") == "compact_boundary":
        return _pick(raw, _ROW_FIELDS)
    attachment = raw.get("attachment")
    if (
        row_type == "attachment"
        and is_object_dict(attachment)
        and attachment.get("type") == "queued_command"
    ):
        kept = _pick(raw, _ROW_FIELDS)
        kept["attachment"] = _capped(_pick(attachment, _QUEUED_FIELDS), "prompt")
        return kept
    return None


def _pick(raw: Mapping[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    return {field: raw[field] for field in fields if field in raw}


def _conversation(raw: Mapping[str, object]) -> dict[str, object]:
    kept = _pick(raw, _ROW_FIELDS)
    result = raw.get("toolUseResult")
    if is_object_dict(result):
        kept["toolUseResult"] = _pick(result, _TOOL_RESULT_FIELDS)
    message = raw.get("message")
    if is_object_dict(message):
        kept["message"] = _message(message)
    return kept


def _message(message: Mapping[str, object]) -> dict[str, object]:
    kept = _pick(message, _MESSAGE_FIELDS)
    usage = message.get("usage")
    if is_object_dict(usage):
        kept["usage"] = _pick(usage, _USAGE_FIELDS)
    content = message.get("content")
    if isinstance(content, str):
        kept["content"] = _cut(content)
        if _over(content):
            kept["truncated_from_bytes"] = len(content.encode())
    elif is_object_list(content):
        kept["content"] = [_block(block) for block in content]
    return kept


def _block(block: object) -> object:
    if not is_object_dict(block):
        return block
    kind = block.get("type")
    if kind == "image":
        return _image(block)
    if kind == "text":
        return _capped({"type": "text", "text": block.get("text")}, "text")
    if kind == "thinking":
        return _capped({"type": "thinking", "thinking": block.get("thinking")}, "thinking")
    if kind == "tool_use":
        return {
            "type": "tool_use",
            "id": block.get("id"),
            "name": block.get("name"),
            "input": _bounded(block.get("input")),
        }
    if kind == "tool_result":
        return _capped(
            {
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id"),
                "is_error": block.get("is_error"),
                "content": _result_content(block.get("content")),
            },
            "content",
        )
    return {"type": kind}


def _result_content(content: object) -> object:
    if isinstance(content, str):
        return content
    if is_object_list(content):
        return [_block(block) for block in content]
    return None


def _image(block: Mapping[str, object]) -> dict[str, object]:
    source = block.get("source")
    data = source.get("data") if is_object_dict(source) else None
    media = source.get("media_type") if is_object_dict(source) else None
    raw = data.encode() if isinstance(data, str) else b""
    return {
        "type": "image",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "media_type": media,
    }


def _over(value: object) -> bool:
    return isinstance(value, str) and len(value.encode()) > ROW_TEXT_CAP_BYTES


def _cut(value: str) -> str:
    if not _over(value):
        return value
    return value.encode()[:ROW_TEXT_CAP_BYTES].decode(errors="ignore")


def _capped(fields: dict[str, object], name: str) -> dict[str, object]:
    value = fields.get(name)
    if isinstance(value, str) and _over(value):
        return {**fields, name: _cut(value), "truncated_from_bytes": len(value.encode())}
    return fields


def _bounded(value: object) -> object:
    if not is_object_dict(value):
        return value
    return {key: _cut(item) if isinstance(item, str) else item for key, item in value.items()}
