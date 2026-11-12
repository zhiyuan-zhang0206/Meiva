from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.session.jsonl.store_text import SCALAR_CAP_BYTES, cut

# meiva:note
# The two shapes a stored row keeps instead of the thing itself, for every adapter: an image becomes
# `{sha256, bytes, media_type}` (its bytes are never stored), and a tool input over the cap becomes a shallow
# summary (its scalar and short values, every long string cut, nested structures that are not short replaced
# by their type and size) carrying `INPUT_SIZE_KEY`, its original size.
# meiva:link base/lm/harness/docs/transcript-projection.md

NESTED_KEEP_BYTES = 2_048
INPUT_SIZE_KEY = "meiva_truncated_from_bytes"
_INPUT_KEYS_KEPT = 32
_KEY_CAP_BYTES = 64


def image_stub(data: bytes, media_type: str | None) -> dict[str, object]:
    return {
        "type": "image",
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "media_type": cut(media_type, SCALAR_CAP_BYTES) if media_type is not None else None,
    }


def _json_size(value: object) -> int | None:
    try:
        return len(json.dumps(value, ensure_ascii=False).encode(errors="replace"))
    except (RecursionError, ValueError, TypeError):
        return None


def bounded_input(value: object, cap: int) -> object:
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
        "keys": [cut(key, _KEY_CAP_BYTES) for key in list(value)[:_INPUT_KEYS_KEPT]],
        INPUT_SIZE_KEY: size,
    }


def _summary(value: Mapping[str, object], cap: int) -> dict[str, object]:
    strings = sum(1 for item in value.values() if isinstance(item, str))
    budget = max(cap // max(strings, 1), SCALAR_CAP_BYTES)
    return {key: _summarized(item, budget) for key, item in value.items()}


def _summarized(item: object, budget: int) -> object:
    if isinstance(item, str):
        return cut(item, budget)
    if item is None or isinstance(item, bool | int | float):
        return item
    nested = _json_size(item)
    if nested is not None and nested <= NESTED_KEEP_BYTES:
        return item
    return {"type": type(item).__name__, "bytes": nested}
