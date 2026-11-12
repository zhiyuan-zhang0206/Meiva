from __future__ import annotations

import re
from collections.abc import Mapping

from base.host.boundary.json_values import is_object_dict, is_object_list

# meiva:note
# How a harness CLI's text is made storable for the transcript rows kept in our database, the same for every
# adapter: each adapter's own whitelist decides which fields are kept, and this decides how a kept string is
# cut and cleaned.
#
# Size, per text field: a main record's field is cut at `MAIN_TEXT_CAP_BYTES`, 256 KiB, which keeps today's
# behaviour that the main agent's own output is not truncated on the timeline; a sub-agent record's field is
# cut at `SUBAGENT_TEXT_CAP_BYTES`, 16 KiB, about twice the 8,000 characters the timeline shows of a sub-agent's
# item (`SUBAGENT_ITEM_CHARS` in `agent/graph/claim/harness_activity.py`; `base` cannot import it), because a
# sub-agent's tool output is the bulk of a long session. A cut field keeps its head and gains
# `truncated_from_bytes`, the original length (`mark_truncated`). A tool input is bounded as a whole
# (`bounded_input`): over the same cap it is replaced by a shallow summary (its scalar and short values, every
# long string cut, nested structures that are not short replaced by their type and size) carrying
# `INPUT_SIZE_KEY`, its original size. Image bytes become `{sha256, bytes, media_type}` (`image_stub`).
#
# Whatever reaches the database must be storable as `jsonb`: `scrub` removes the NUL character and replaces a
# lone surrogate, which `jsonb` refuses and which would otherwise roll back the write of the batch and keep
# the position from moving, so the writer would stay on that row for good. `scrub` is iterative, so nesting
# depth cannot exhaust the stack.
# meiva:link base/lm/harness/docs/transcript-projection.md

MAIN_TEXT_CAP_BYTES = 262_144
SUBAGENT_TEXT_CAP_BYTES = 16_384
SCALAR_CAP_BYTES = 1_024
_DIRTY = re.compile("[\x00\ud800-\udfff]")


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


def scalar(value: object) -> object:
    if isinstance(value, str):
        return cut(value, SCALAR_CAP_BYTES)
    return value if value is None or isinstance(value, bool | int | float) else None


def scalars(raw: Mapping[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    kept: dict[str, object] = {}
    for field in fields:
        value = raw.get(field)
        if isinstance(value, str):
            kept[field] = cut(value, SCALAR_CAP_BYTES)
        elif field in raw and (value is None or isinstance(value, bool | int | float)):
            kept[field] = value
    return kept


def _byte_size(value: str) -> int:
    return len(value.encode(errors="replace"))


def cut(value: str, cap: int) -> str:
    if len(value) <= cap // 4 or _byte_size(value) <= cap:
        return value
    return value.encode(errors="replace")[:cap].decode(errors="ignore")


def mark_truncated(fields: dict[str, object], name: str, original: object) -> None:
    kept = fields.get(name)
    if isinstance(original, str) and isinstance(kept, str) and kept != original:
        fields["truncated_from_bytes"] = _byte_size(original)


def text_field(
    fields: dict[str, object], name: str, cap: int, *, original: object | None = None
) -> dict[str, object]:
    source = fields.get(name) if original is None else original
    if isinstance(source, str):
        fields[name] = cut(source, cap)
        mark_truncated(fields, name, source)
    elif original is None:
        fields[name] = None
    return fields
