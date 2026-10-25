# meiva:note
# How a keyed operation is claimed, replayed and retried, as functions over values.
#
# `daemon.dispatch_idempotent_pass` owns the effects (the claim insert, the dispatch, the stored
# result). A committed claim that never stored a result stays pending: pending means uncertain
# and never authorizes a second execution. What the stored row says is decided here:
#
# - the identity of a request (its kind and payload, independent of key order),
# - what a waiter does with the stored row: wait on, replay the result, or refuse a different
#   request that reused the key,
# - the failed results of a refused key and of an owner that never completed,
# - whether a pass that died on a lost connection is attempted again.

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard

from ops.rpc_schemas import OpStatus

__all__ = [
    "Conflict",
    "Replay",
    "Stored",
    "Wait",
    "conflict_failure",
    "request_hash",
    "retry_pass",
    "stored_body",
    "stored_verdict",
    "uncertain_failure",
]


# meiva:note
# The row stored under the key belongs to an owner that has not stored a result: wait for it.
@dataclass(frozen=True)
class Wait:
    pass


# meiva:note
# The row stored under the key belongs to another request: refuse without executing.
@dataclass(frozen=True)
class Conflict:
    pass


# meiva:note
# The row stored under the key holds the owner's outcome: answer it again.
@dataclass(frozen=True)
class Replay:
    status: OpStatus
    result: dict[str, object]


Stored = Wait | Conflict | Replay


# meiva:note
# The identity of a request: the SHA-256 of its kind and payload with sorted keys, so the same
# request spelled in another key order is the same request.
def request_hash(kind: str, payload: Mapping[str, object]) -> str:
    encoded = json.dumps([kind, payload], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


# meiva:note
# The text a payload or a result is stored as; a value that is not JSON-native is stored as its
# `str()`.
def stored_body(value: Mapping[str, object]) -> str:
    return json.dumps(value, default=str)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# The verdict on the row stored under a key, read as (method, path, request_hash, op_status,
# response_body), or None when there is none yet.
#
# A row of another method, kind or request identity is a conflict, which includes a row written
# before identities were recorded. A row without a status belongs to an owner still running or
# one that never finished. A status outside the vocabulary raises.
def stored_verdict(row: Sequence[object] | None, kind: str, digest: str) -> Stored:
    if row is None:
        return Wait()
    method, path, stored_digest, status, body = row
    if method != "ops" or path != kind or stored_digest != digest:
        return Conflict()
    if status is None:
        return Wait()
    result = dict(body) if _is_object_dict(body) else {}
    return Replay(OpStatus(str(status)), result)


# meiva:note
# The failed result of a key that was used by a different request.
def conflict_failure() -> tuple[OpStatus, dict[str, object]]:
    return OpStatus.FAILED, {
        "error": "idempotency request identity conflict or legacy identity unavailable; "
        "the stored operation was not re-executed"
    }


# meiva:note
# The failed result of a key whose owner never stored a result.
def uncertain_failure(key: str) -> tuple[OpStatus, dict[str, object]]:
    return OpStatus.FAILED, {
        "error": f"idempotency key {key!r} is owned by another dispatch that never "
        "completed; outcome uncertain, inspect the stored operation before recovery"
    }


# meiva:note
# Whether a pass that died on a lost connection is attempted again: only while attempts are left.
def retry_pass(failed_attempts: int, attempts: int) -> bool:
    return failed_attempts < attempts
