# meiva:note
# Typed JSON from an HTTP answer.
#
# `httpx.Response.json()` returns `Any`. These read the body through `cli.boundary.json_text.loads`
# (pydantic's `JsonValue`), so a reader narrows the value with `isinstance` before using it. A body
# that is not JSON raises `json.JSONDecodeError`; one that is JSON of another container than the
# caller asks for raises `JsonShapeError`, a `ValueError`. `loads` is re-exported for callers that
# read both an answer and a text.

from __future__ import annotations

import httpx
from pydantic import JsonValue

from cli.boundary.json_text import loads

__all__ = ["JsonShapeError", "loads", "response_list", "response_object", "response_value"]


# meiva:note
# A JSON body that parsed but is not the container the caller asked for.
class JsonShapeError(ValueError):
    pass


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
