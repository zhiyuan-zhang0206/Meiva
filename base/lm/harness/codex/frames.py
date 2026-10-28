from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

type JsonValue = (
    str | int | float | bool | dict[str, Any] | list[JsonValue] | Mapping[str, Any] | None
)
type JsonObject = dict[str, Any]
type RequestParams = Mapping[str, Any]

# meiva:note
# A JSON-RPC id is a number, a string or null; the ids this client mints are numbers, and only a
# reply to one of them can be correlated.
type RequestId = int | str | None

JSONRPC = "2.0"

# meiva:note
# The two refusals this client answers a server request with: it has no handler for the method, or
# the handler raised. Both are JSON-RPC's own codes, so the server reads one request as refused
# instead of reading the stream as broken.
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

RequestHandler = Callable[[str, JsonObject], JsonObject | Awaitable[JsonObject]]
NotificationHandler = Callable[[str, JsonObject], None]
ClosedHandler = Callable[[str], None]


# meiva:note
# `TransportClosedError` means the child is gone, so no further request can be
# answered; `JsonRpcError` carries a server-side refusal the caller may recover
# from (a rejected `thread/resume`, for example).
class TransportClosedError(RuntimeError):
    pass


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"app-server error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def as_object(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def as_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


# meiva:note
# `True` and `False` are ints in Python, and neither is a token count or a JSON-RPC id.
def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def request_frame(request_id: int, method: str, params: RequestParams) -> JsonObject:
    return {"jsonrpc": JSONRPC, "id": request_id, "method": method, "params": dict(params)}


def notification_frame(method: str, params: RequestParams | None) -> JsonObject:
    frame: JsonObject = {"jsonrpc": JSONRPC, "method": method}
    if params is not None:
        frame["params"] = dict(params)
    return frame


# meiva:note
# One JSON object per line on the child's stdin: the compact form, because a payload can be a full
# command output and the separators are pure overhead on every line. `ensure_ascii` stays on so a
# line is always the child's own encoding rather than whatever the payload happened to carry.
def encode(message: JsonObject) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode()


# meiva:note
# A request of the server's: an approval, answered on the same pipe or the turn parks forever.
@dataclass(frozen=True, slots=True)
class ServerRequest:
    request_id: RequestId
    method: str
    params: JsonObject

    def answered(self, result: JsonObject) -> JsonObject:
        return {"jsonrpc": JSONRPC, "id": self.request_id, "result": result}

    def refused(self) -> JsonObject:
        return self._failed(METHOD_NOT_FOUND, "no handler")

    def failed(self, reason: object) -> JsonObject:
        return self._failed(INTERNAL_ERROR, str(reason))

    def _failed(self, code: int, message: str) -> JsonObject:
        return {
            "jsonrpc": JSONRPC,
            "id": self.request_id,
            "error": {"code": code, "message": message},
        }


# meiva:note
# An answer to a request this client sent. `outcome` is what settles that request: the result
# object (an empty one when the server sent none), or the refusal it carries, which a caller may
# recover from.
@dataclass(frozen=True, slots=True)
class Reply:
    request_id: int
    frame: JsonObject

    def outcome(self) -> JsonObject | JsonRpcError:
        error = self.frame.get("error")
        if error is None:
            return as_object(self.frame.get("result"))
        details = as_object(error)
        return JsonRpcError(
            _as_int(details.get("code")) or -32000,
            as_text(details.get("message")) or "unknown",
            details.get("data"),
        )


@dataclass(frozen=True, slots=True)
class Notification:
    method: str
    params: JsonObject


# meiva:note
# What one line of the server's stdout is. A line that is not a JSON object says nothing and is
# dropped; a frame with an `id` and a `method` is a request of the server's, one with a `method`
# alone is a notification, and one with an integer `id` alone answers a request of ours.
def classify(line: bytes) -> ServerRequest | Reply | Notification | None:
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError:
        return None
    message = as_object(decoded)
    if not message:
        return None
    if "id" in message and "method" in message:
        return ServerRequest(
            message.get("id"),
            as_text(message.get("method")) or "",
            as_object(message.get("params")),
        )
    if "method" in message:
        return Notification(as_text(message["method"]) or "", as_object(message.get("params")))
    request_id = _as_int(message.get("id"))
    if request_id is None:
        return None
    return Reply(request_id, message)
