# meiva:note
# Typed JSON from a text a command received.
#
# `json.loads` returns `Any`. This reads the same text through pydantic's `JsonValue`, so a reader
# holds a value of the JSON types and narrows it with `isinstance` (a `dict` is
# `dict[str, JsonValue]`) before using it. Text that is not JSON raises `json.JSONDecodeError`, what
# `json.loads` raised, so callers that catch it are unchanged.

from __future__ import annotations

import json

from pydantic import JsonValue, TypeAdapter, ValidationError

__all__ = ["loads"]

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


# meiva:note
# The JSON value in `text`.
def loads(text: str | bytes) -> JsonValue:
    try:
        return _JSON_VALUE.validate_json(text)
    except ValidationError as exc:
        document = text if isinstance(text, str) else text.decode("utf-8", errors="replace")
        raise json.JSONDecodeError(exc.errors()[0]["msg"], document, 0) from exc
