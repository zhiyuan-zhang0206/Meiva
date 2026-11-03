# meiva:note
# Typed JSON from an HTTP answer or a text a command received.
#
# `httpx.Response.json()` and `json.loads` return `Any`. These read the same bytes through
# pydantic's `JsonValue`, so a reader holds a value of the JSON types and narrows it with
# `isinstance` (a `dict` is `dict[str, JsonValue]`) before using it. A body that is not JSON, or
# not the container a caller asks for, is a `ValueError`: `json.JSONDecodeError` for text that is not JSON (what
# `json.loads` raised, so callers catching it keep working), `JsonShapeError` for the wrong container.

from __future__ import annotations

import json

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

__all__ = ["JsonShapeError", "loads", "response_list", "response_object", "response_value"]


# meiva:note
# A JSON body that parsed but is not the container the caller asked for.
class JsonShapeError(ValueError):
    pass


# meiva:note
# The JSON value in `text`.
def loads(text: str | bytes) -> JsonValue:
    adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
    try:
        return adapter.validate_json(text)
    except ValidationError as exc:
        document = text if isinstance(text, str) else text.decode("utf-8", errors="replace")
        raise json.JSONDecodeError(exc.errors()[0]["msg"], document, 0) from exc


# meiva:note
# The JSON value in the response body.
def response_value(response: httpx.Response) -> JsonValue:
    return loads(response.content)


# meiva:note
# The JSON object in the response body.
def response_object(response: httpx.Response) -> dict[str, JsonValue]:
    value = response_value(response)
    if not isinstance(value, dict):
        raise JsonShapeError(f"expected a JSON object, got {type(value).__name__}")
    return value


# meiva:note
# The JSON array in the response body.
def response_list(response: httpx.Response) -> list[JsonValue]:
    value = response_value(response)
    if not isinstance(value, list):
        raise JsonShapeError(f"expected a JSON array, got {type(value).__name__}")
    return value
