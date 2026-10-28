from __future__ import annotations

from dataclasses import dataclass

from base.lm.harness.acp.errors import AcpRpcError
from base.lm.harness.acp.frames import JSONRPC, Json, JsonObject, RequestId
from base.lm.harness.acp.permission import PermissionRequest, parse_permission_request

METHOD_NOT_FOUND = -32601


@dataclass(frozen=True, slots=True)
class AcpNotification:
    method: str
    params: JsonObject


# meiva:note
# What an inbound frame is. A frame with an `id` and a `method` is a request of the agent's, one
# with a `method` alone is a notification, one with an integer `id` alone answers a request of
# ours (a result or an error). Anything else says nothing and is dropped.
@dataclass(frozen=True, slots=True)
class ServerRequest:
    request_id: RequestId
    method: str
    params: Json


@dataclass(frozen=True, slots=True)
class Reply:
    request_id: int
    result: JsonObject | None
    error: JsonObject | None


def classify(frame: JsonObject) -> ServerRequest | AcpNotification | Reply | None:
    if "id" in frame and "method" in frame:
        return ServerRequest(frame["id"], str(frame["method"]), frame.get("params"))
    if "method" in frame:
        params: Json = frame.get("params")
        return AcpNotification(
            method=str(frame["method"]), params=params if isinstance(params, dict) else {}
        )
    request_id = frame.get("id")
    if not isinstance(request_id, int):
        return None
    error: Json = frame.get("error")
    if isinstance(error, dict):
        return Reply(request_id, None, error)
    result: Json = frame.get("result")
    return Reply(request_id, result if isinstance(result, dict) else {}, None)


# meiva:note
# What an answer to one of our requests settles it with: the result object (an empty one when the
# agent sent none), or the JSON-RPC error it refused with, named after the method it answered.
def reply_outcome(reply: Reply, method: str) -> JsonObject | AcpRpcError:
    if reply.error is not None:
        return AcpRpcError(
            method,
            int(reply.error.get("code", -1)),
            str(reply.error.get("message", "")),
            reply.error.get("data"),
        )
    return reply.result if reply.result is not None else {}


# meiva:note
# The transport owns no policy: it answers a permission request through the handler and refuses
# every other client-side method with -32601, because silently accepting a surface we do not
# implement would let an agent believe it read a file or ran a command.
@dataclass(frozen=True, slots=True)
class Refusal:
    frame: JsonObject


@dataclass(frozen=True, slots=True)
class AskPermission:
    request: PermissionRequest
    request_id: RequestId


def answer_server_request(request: ServerRequest) -> Refusal | AskPermission:
    if request.method == "session/request_permission" and isinstance(request.params, dict):
        return AskPermission(parse_permission_request(request.params), request.request_id)
    return Refusal(
        {
            "jsonrpc": JSONRPC,
            "id": request.request_id,
            "error": {
                "code": METHOD_NOT_FOUND,
                "message": f"client does not implement {request.method}",
            },
        }
    )
