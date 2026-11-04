from __future__ import annotations

from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.acp.errors import AcpRpcError
from base.lm.harness.acp.frames import JSONRPC, JsonObject, RequestId
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
    params: object


@dataclass(frozen=True, slots=True)
class Reply:
    request_id: int
    result: JsonObject | None
    error: JsonObject | None


def classify(frame: JsonObject) -> ServerRequest | AcpNotification | Reply | None:
    if "id" in frame and "method" in frame:
        server_id = frame["id"]
        if not (server_id is None or isinstance(server_id, int | float | str)):
            return None
        return ServerRequest(server_id, str(frame["method"]), frame.get("params"))
    if "method" in frame:
        params = frame.get("params")
        return AcpNotification(
            method=str(frame["method"]), params=params if is_object_dict(params) else {}
        )
    request_id = frame.get("id")
    if not isinstance(request_id, int):
        return None
    error = frame.get("error")
    if is_object_dict(error):
        return Reply(request_id, None, error)
    result = frame.get("result")
    return Reply(request_id, result if is_object_dict(result) else {}, None)


# meiva:note
# What an answer to one of our requests settles it with: the result object (an empty one when the
# agent sent none), or the JSON-RPC error it refused with, named after the method it answered.
def reply_outcome(reply: Reply, method: str) -> JsonObject | AcpRpcError:
    if reply.error is not None:
        code = reply.error.get("code", -1)
        return AcpRpcError(
            method,
            int(code) if isinstance(code, int | float | str) else -1,
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
    if request.method == "session/request_permission" and is_object_dict(request.params):
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
