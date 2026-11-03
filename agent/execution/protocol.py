# meiva:note
# Exec-subprocess protocol — request/result envelopes and typed (de)serialization
# shared by the parent (`agent/graph/exec/_subprocess.py`) and child (`agent/execution/child.py`).

from __future__ import annotations

import base64
import contextlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeGuard
from uuid import UUID

from pydantic import TypeAdapter

from agent.execution.result_kind import RESULT_KINDS, ResultKind
from base.host.atomic_io import write_bytes_atomic
from base.host.boundary.fields import (
    optional_bool_field,
    optional_float_field,
    optional_int_field,
    optional_list_field,
    optional_object_field,
    optional_str_field,
    str_field,
)
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation

REQUEST_VERSION = 1
RESULT_VERSION = 1


MAX_ENVELOPE_BYTES = 64 * 1024 * 1024
STALE_FILE_AGE_S = 3600.0
_NO_AGENT_DIRNAME = "_no_agent_"


# meiva:note
# The pair `JsonPlusSerializer.dumps_typed` answers, checked rather than declared: langgraph leaves
# the method unannotated, so this validator is what turns its `Any` into a real tuple.
_TYPED_BLOB = TypeAdapter(tuple[str, bytes])


# meiva:note
# Lazily build the typed-blob serializer using the same msgpack allowlist as the checkpointer.
def dumps_typed(obj: object) -> tuple[str, bytes]:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from agent.state import checkpoint_msgpack_allowlist
    from base.packages.plugins.loading.registry import loaded_state_classes

    serde = JsonPlusSerializer(
        allowed_msgpack_modules=checkpoint_msgpack_allowlist(loaded_state_classes())
    )
    return _TYPED_BLOB.validate_python(object_of(serde.dumps_typed, obj))


def loads_typed(data: tuple[str, bytes]) -> object:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from agent.state import checkpoint_msgpack_allowlist
    from base.packages.plugins.loading.registry import loaded_state_classes

    serde = JsonPlusSerializer(
        allowed_msgpack_modules=checkpoint_msgpack_allowlist(loaded_state_classes())
    )
    return object_of(serde.loads_typed, data)


@dataclass
class RequestPayload:
    code: str
    context: Mapping[str, object]
    timeout_s: float
    state: dict[str, object] | None
    incarnation: RuntimeIncarnation | None = None
    state_raw: tuple[str, bytes] | None = None

    @property
    def agent_id(self) -> int | None:
        return _acting_agent_id(self.context)

    def materialize_state(self) -> dict[str, object] | None:
        if self.state_raw is None:
            return self.state
        if self.state is None:
            decoded = loads_typed(self.state_raw)
            if not is_object_dict(decoded):
                raise ValueError(
                    f"exec request state blob decoded to {type(decoded).__name__}, expected dict"
                )
            self.state = decoded
        return self.state


@dataclass
class ResultPayload:
    kind: ResultKind
    lifecycle_type: str | None = None
    exc_type: str | None = None
    exc_msg: str | None = None
    full_traceback: str | None = None
    code_reached: bool | None = None
    state_update: dict[str, object] | None = None
    state_update_error: str | None = None
    sdk_calls: list[dict[str, object]] | None = None


# meiva:note
# The agent id the request context acts as: `identity.agent_id` is the one field both sides write
# (`MeivaContext.describe`) and both sides read (the payload, and the envelope check below), so a
# missing or non-object identity fails here instead of at two separate indexing sites.
def _acting_agent_id(context: Mapping[str, object]) -> int | None:
    identity = optional_object_field(context, "identity")
    if identity is None:
        raise TypeError("exec request context identity must be an object")
    return optional_int_field(identity, "agent_id")


# meiva:note
# The number `row[key]`; `base.host.boundary.fields` has `int_field` but no float twin, and a
# timeout reads as a float.
def _float_field(row: Mapping[str, object], key: str) -> float:
    value = optional_float_field(row, key)
    if value is None:
        raise TypeError(f"{key} is {type(row.get(key)).__name__}, not a number")
    return value


# meiva:note
# The request envelope path of one exec call; `request_id` is that call's own identity.
def make_request_path(exec_dir: Path, agent_id: int | None, *, request_id: UUID) -> Path:
    return _agent_dir(exec_dir, agent_id) / f"req-{request_id.hex}.json"


# meiva:note
# The result envelope path of one exec call; `result_id` is that call's own identity.
def make_result_path(exec_dir: Path, agent_id: int | None, *, result_id: UUID) -> Path:
    return _agent_dir(exec_dir, agent_id) / f"res-{result_id.hex}.json"


def _agent_dir(exec_dir: Path, agent_id: int | None) -> Path:
    d = exec_dir / (str(agent_id) if agent_id is not None else _NO_AGENT_DIRNAME)
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        d.chmod(0o700)
    return d


# meiva:note
# Remove the result files and write scratch an exec left over an hour before `now`. Request
# envelopes stay: a dead call's request is evidence of what it was asked to run.
def prune_stale(exec_dir: Path, agent_id: int | None, *, now: datetime) -> None:
    agent_dir = _agent_dir(exec_dir, agent_id)
    cutoff = now.timestamp() - STALE_FILE_AGE_S
    for path in (*agent_dir.glob("*.json"), *agent_dir.glob(".*.tmp")):
        if path.name.startswith("req-"):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except FileNotFoundError:
            continue


def write_request(
    path: Path,
    *,
    code: str,
    context: Mapping[str, object],
    timeout_s: float,
    state: Mapping[str, object] | None,
    incarnation: RuntimeIncarnation | None,
) -> None:
    started_at = time.perf_counter()
    agent_id = _acting_agent_id(context)
    envelope: dict[str, object] = {
        "v": REQUEST_VERSION,
        "code": code,
        "agent_id": agent_id,
        "context": context,
        "timeout_s": timeout_s,
    }
    if incarnation is not None:
        if agent_id is None:
            raise RuntimeError("an exec incarnation needs an agent id")
        incarnation.require_agent(agent_id)
        envelope["incarnation"] = {
            "generation": str(incarnation.generation),
            "owner": str(incarnation.owner),
        }
    if state is not None:
        tag, blob = dumps_typed(state)
        envelope["state_tag"] = tag
        envelope["state_b64"] = base64.b64encode(blob).decode("ascii")
    _write_json(path, envelope)
    _log_envelope_transfer("request", "write", path, started_at)


def read_request(path: Path) -> RequestPayload:
    started_at = time.perf_counter()
    envelope = _read_json(path)
    version = optional_int_field(envelope, "v")
    if version != REQUEST_VERSION:
        raise ValueError(
            f"exec request envelope version {version!r} != {REQUEST_VERSION} "
            f"(stale file or version skew between agent and exec child)"
        )
    context = optional_object_field(envelope, "context")
    if context is None:
        raise TypeError("exec request envelope context must be a dict")
    state_raw = None
    state_b64 = optional_str_field(envelope, "state_b64")
    if state_b64 is not None:
        state_raw = (str_field(envelope, "state_tag"), base64.b64decode(state_b64))
    acting_agent_id = _acting_agent_id(context)
    envelope_agent_id = optional_int_field(envelope, "agent_id")
    if envelope_agent_id != acting_agent_id:
        raise ValueError(
            f"exec request envelope names agent {envelope_agent_id!r} but its context acts as "
            f"{acting_agent_id!r}"
        )
    identity = optional_object_field(envelope, "incarnation")
    incarnation = None
    if identity is not None:
        if envelope_agent_id is None:
            raise ValueError("exec incarnation requires an agent id")
        incarnation = RuntimeIncarnation(
            envelope_agent_id,
            UUID(str_field(identity, "generation")),
            UUID(str_field(identity, "owner")),
        )
    payload = RequestPayload(
        code=str_field(envelope, "code"),
        context=context,
        timeout_s=_float_field(envelope, "timeout_s"),
        state=None,
        incarnation=incarnation,
        state_raw=state_raw,
    )
    _log_envelope_transfer("request", "read", path, started_at)
    return payload


def write_result(path: Path, payload: ResultPayload) -> None:
    if payload.kind not in RESULT_KINDS:
        raise ValueError(f"unknown result kind {payload.kind!r}")
    started_at = time.perf_counter()
    envelope: dict[str, object] = {
        "v": RESULT_VERSION,
        "kind": payload.kind,
        "lifecycle_type": payload.lifecycle_type,
        "exc_type": payload.exc_type,
        "exc_msg": payload.exc_msg,
        "full_traceback": payload.full_traceback,
        "code_reached": payload.code_reached,
        "state_update_error": payload.state_update_error,
        "sdk_calls": payload.sdk_calls,
    }
    if payload.state_update is not None:
        tag, blob = dumps_typed(payload.state_update)
        envelope["update_tag"] = tag
        envelope["update_b64"] = base64.b64encode(blob).decode("ascii")
    _write_json(path, envelope)
    _log_envelope_transfer("result", "write", path, started_at)


def _is_result_kind(value: object) -> TypeGuard[ResultKind]:
    return isinstance(value, str) and value in RESULT_KINDS


def read_result(path: Path) -> ResultPayload:
    started_at = time.perf_counter()
    envelope = _read_json(path)
    version = optional_int_field(envelope, "v")
    if version != RESULT_VERSION:
        raise ValueError(f"exec result envelope version {version!r} != {RESULT_VERSION}")
    kind_raw = envelope.get("kind")
    if not _is_result_kind(kind_raw):
        raise ValueError(f"exec result envelope has unknown kind {kind_raw!r}")
    state_update: dict[str, object] | None = None
    update_b64 = optional_str_field(envelope, "update_b64")
    if update_b64 is not None:
        decoded = loads_typed((str_field(envelope, "update_tag"), base64.b64decode(update_b64)))
        if not is_object_dict(decoded):
            raise ValueError(
                f"exec result update blob decoded to {type(decoded).__name__}, expected dict"
            )
        state_update = decoded
    payload = ResultPayload(
        kind=kind_raw,
        lifecycle_type=optional_str_field(envelope, "lifecycle_type"),
        exc_type=optional_str_field(envelope, "exc_type"),
        exc_msg=optional_str_field(envelope, "exc_msg"),
        full_traceback=optional_str_field(envelope, "full_traceback"),
        code_reached=optional_bool_field(envelope, "code_reached"),
        state_update=state_update,
        state_update_error=optional_str_field(envelope, "state_update_error"),
        sdk_calls=_sdk_calls(envelope),
    )
    _log_envelope_transfer("result", "read", path, started_at)
    return payload


# meiva:note
# The wire list `base.agents.sdk.telemetry.tally_entries` writes: one object per SDK call, or None
# when the child wrote none. An entry that is not an object is dropped, as before.
def _sdk_calls(envelope: Mapping[str, object]) -> list[dict[str, object]] | None:
    entries = optional_list_field(envelope, "sdk_calls")
    if entries is None:
        return None
    return [entry for entry in entries if is_object_dict(entry)]


def _log_envelope_transfer(
    envelope: Literal["request", "result"],
    op: Literal["read", "write"],
    path: Path,
    started_at: float,
) -> None:
    size_bytes = path.stat().st_size
    serialize_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "[exec envelope] {op} {envelope} size_bytes={size_bytes} serialize_ms={serialize_ms:.1f}",
        event="exec_envelope",
        envelope=envelope,
        op=op,
        size_bytes=size_bytes,
        serialize_ms=serialize_ms,
    )


def _write_json(path: Path, envelope: Mapping[str, object]) -> None:
    data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    write_bytes_atomic(path, data, mode=0o600, suppress_cleanup_error=True)


def _read_json(path: Path) -> dict[str, object]:
    size_bytes = path.stat().st_size
    if size_bytes > MAX_ENVELOPE_BYTES:
        raise ValueError(
            f"exec envelope {path} is {size_bytes} bytes, over the {MAX_ENVELOPE_BYTES} "
            "ceiling — this state snapshot or result delta is over the exec envelope limit; "
            "compact the conversation"
        )
    data = object_of(json.loads, path.read_text(encoding="utf-8"))
    if not is_object_dict(data):
        raise TypeError(f"exec envelope {path} is {type(data).__name__}, expected object")
    return data
