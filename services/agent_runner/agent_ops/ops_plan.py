# meiva:note
# What the ops daemon says on the wire, as functions over values.
#
# `daemon.ops_route` owns the effects (admission, the semaphore, the dispatch). Every decision
# about bytes and failures lives here, so a test passes a request body or an exception and asserts
# the returned reply or failed result:
#
# - which requests are refused before they are dispatched (malformed JSON, a bad envelope, an
#   unknown kind) and with which status,
# - the `{status, result}` envelope a finished operation is answered with,
# - the failed result an exception becomes (an HTTP 200 carrying a failed result, so a caller tells
#   it from transport failure).

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from base.api_contracts.agent_contract import MeivaAgentError
from ops.rpc_schemas import OpEnvelope, OpStatus, is_op_kind

__all__ = [
    "OpReply",
    "failure_result",
    "op_reply",
    "read_request",
    "unknown_kind",
]

_JSON = "application/json"


# meiva:note
# One HTTP answer of the ops route.
@dataclass(frozen=True)
class OpReply:
    status_code: int
    body: bytes
    content_type: str


# meiva:note
# The failed result of a kind the wire vocabulary does not contain.
def unknown_kind(kind: str) -> tuple[OpStatus, dict[str, object]]:
    return OpStatus.FAILED, {"error": f"unknown kind: {kind!r}"}


# meiva:note
# The `{status, result}` envelope of a finished operation at HTTP 200.
#
# A failed operation is a semantic outcome the caller re-raises, not an HTTP error. A value in
# the result that is not JSON-native degrades to its `str()` instead of breaking the control
# plane's answer.
def op_reply(status: OpStatus, result: Mapping[str, object]) -> OpReply:
    body = json.dumps({"status": status, "result": result}, default=str).encode()
    return OpReply(200, body, _JSON)


def _bad_request(message: str) -> OpReply:
    return OpReply(400, json.dumps({"error": message}).encode(), _JSON)


# meiva:note
# The envelope of a request body, or the answer that refuses it: 400 for a body that is not JSON
# or not `{kind, payload}`, and a failed result at 200 for a kind outside the vocabulary.
def read_request(body: bytes) -> OpEnvelope | OpReply:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        return _bad_request(f"invalid JSON body: {exc}")
    try:
        envelope = OpEnvelope.model_validate(parsed)
    except ValidationError as exc:
        return _bad_request(f"body must be {{kind: str, payload: dict}}: {exc}")
    if not is_op_kind(envelope.kind):
        status, result = unknown_kind(envelope.kind)
        return op_reply(status, result)
    return envelope


# meiva:note
# The failed result of an exception: an agent error carries its reason, any other only its type
# and message. Which exceptions are outcomes of an operation, and which are crashes, is the
# caller's `except` clause; both answer the caller with a failed result.
def failure_result(exc: Exception) -> tuple[OpStatus, dict[str, object]]:
    if isinstance(exc, MeivaAgentError):
        return OpStatus.FAILED, {
            "error": f"{type(exc).__name__}: {exc}",
            "detail": str(exc),
            "reason": exc.reason.value,
        }
    return OpStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}
