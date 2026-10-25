# meiva:note
# Exec-subprocess protocol — request/result envelopes and typed (de)serialization
# shared by the parent (`agent/graph/exec/_subprocess.py`) and child (`agent/execution/child.py`).

from __future__ import annotations

import base64
import contextlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeGuard

from agent.execution.result_kind import RESULT_KINDS, ResultKind
from base.host.atomic_io import write_bytes_atomic
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation

REQUEST_VERSION = 1
RESULT_VERSION = 1


MAX_ENVELOPE_BYTES = 64 * 1024 * 1024
STALE_FILE_AGE_S = 3600.0
_NO_AGENT_DIRNAME = "_no_agent_"


# meiva:note
# Lazily build the typed-blob serializer using the same msgpack allowlist as the checkpointer.
def dumps_typed(obj: object) -> tuple[str, bytes]:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from agent.state import checkpoint_msgpack_allowlist
    from base.packages.plugins.loading.registry import loaded_state_classes

    serde = JsonPlusSerializer(
        allowed_msgpack_modules=checkpoint_msgpack_allowlist(loaded_state_classes())
    )
    return serde.dumps_typed(obj)


def loads_typed(data: tuple[str, bytes]) -> object:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from agent.state import checkpoint_msgpack_allowlist
    from base.packages.plugins.loading.registry import loaded_state_classes

    serde = JsonPlusSerializer(
        allowed_msgpack_modules=checkpoint_msgpack_allowlist(loaded_state_classes())
    )
    return serde.loads_typed(data)


@dataclass
class RequestPayload:
    code: str
    context: dict[str, Any]
    timeout_s: float
    state: dict[str, Any] | None
    incarnation: RuntimeIncarnation | None = None
    state_raw: tuple[str, bytes] | None = None

    @property
    def agent_id(self) -> int | None:
        raw = self.context["identity"]["agent_id"]
        return int(raw) if raw is not None else None

    def materialize_state(self) -> dict[str, Any] | None:
        if self.state_raw is None:
            return self.state
        if self.state is None:
            decoded = loads_typed(self.state_raw)
            if not isinstance(decoded, dict):
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
    state_update: dict[str, Any] | None = None
    state_update_error: str | None = None
    sdk_calls: list[dict[str, Any]] | None = None


def make_request_path(exec_dir: Path, agent_id: int | None) -> Path:
    agent_dir = _agent_dir(exec_dir, agent_id)
    _prune_stale(agent_dir)
    return agent_dir / f"req-{uuid.uuid4().hex}.json"


def make_result_path(exec_dir: Path, agent_id: int | None) -> Path:
    return _agent_dir(exec_dir, agent_id) / f"res-{uuid.uuid4().hex}.json"


def _agent_dir(exec_dir: Path, agent_id: int | None) -> Path:
    d = exec_dir / (str(agent_id) if agent_id is not None else _NO_AGENT_DIRNAME)
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        d.chmod(0o700)
    return d


def _prune_stale(agent_dir: Path) -> None:
    cutoff = time.time() - STALE_FILE_AGE_S
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
    context: dict[str, Any],
    timeout_s: float,
    state: dict[str, Any] | None,
    incarnation: RuntimeIncarnation | None,
) -> None:
    started_at = time.perf_counter()
    agent_id = context["identity"]["agent_id"]
    envelope: dict[str, Any] = {
        "v": REQUEST_VERSION,
        "code": code,
        "agent_id": agent_id,
        "context": context,
        "timeout_s": timeout_s,
    }
    if incarnation is not None:
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
    if envelope.get("v") != REQUEST_VERSION:
        raise ValueError(
            f"exec request envelope version {envelope.get('v')!r} != {REQUEST_VERSION} "
            f"(stale file or version skew between agent and exec child)"
        )
    state_raw = None
    if envelope.get("state_b64") is not None:
        state_raw = (str(envelope["state_tag"]), base64.b64decode(envelope["state_b64"]))
    from uuid import UUID

    acts_as = envelope["context"]["identity"]["agent_id"]
    if envelope["agent_id"] != acts_as:
        raise ValueError(
            f"exec request envelope names agent {envelope['agent_id']!r} but its context acts as "
            f"{acts_as!r}"
        )
    identity = envelope.get("incarnation")
    incarnation = None
    if identity is not None:
        if envelope["agent_id"] is None:
            raise ValueError("exec incarnation requires an agent id")
        incarnation = RuntimeIncarnation(
            int(envelope["agent_id"]), UUID(identity["generation"]), UUID(identity["owner"])
        )
    raw_context: object = envelope["context"]
    if not _is_object_dict(raw_context):
        raise TypeError("exec request envelope context must be a dict")
    payload = RequestPayload(
        code=str(envelope["code"]),
        context={str(k): v for k, v in raw_context.items()},
        timeout_s=float(envelope["timeout_s"]),
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
    envelope: dict[str, Any] = {
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


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def read_result(path: Path) -> ResultPayload:
    started_at = time.perf_counter()
    envelope = _read_json(path)
    if envelope.get("v") != RESULT_VERSION:
        raise ValueError(f"exec result envelope version {envelope.get('v')!r} != {RESULT_VERSION}")
    kind_raw = envelope.get("kind")
    if not _is_result_kind(kind_raw):
        raise ValueError(f"exec result envelope has unknown kind {kind_raw!r}")
    state_update: dict[str, Any] | None = None
    if envelope.get("update_b64") is not None:
        decoded = loads_typed(
            (str(envelope["update_tag"]), base64.b64decode(envelope["update_b64"]))
        )
        if not _is_object_dict(decoded):
            raise ValueError(
                f"exec result update blob decoded to {type(decoded).__name__}, expected dict"
            )
        state_update = {str(k): v for k, v in decoded.items()}
    code_reached = envelope.get("code_reached")
    raw_sdk_calls: object = envelope.get("sdk_calls")
    sdk_calls: list[dict[str, Any]] | None = (
        [{str(k): v for k, v in item.items()} for item in raw_sdk_calls if _is_object_dict(item)]
        if _is_object_list(raw_sdk_calls)
        else None
    )
    payload = ResultPayload(
        kind=kind_raw,
        lifecycle_type=envelope.get("lifecycle_type"),
        exc_type=envelope.get("exc_type"),
        exc_msg=envelope.get("exc_msg"),
        full_traceback=envelope.get("full_traceback"),
        code_reached=(code_reached if isinstance(code_reached, bool) else None),
        state_update=state_update,
        state_update_error=envelope.get("state_update_error"),
        sdk_calls=sdk_calls,
    )
    _log_envelope_transfer("result", "read", path, started_at)
    return payload


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


def _write_json(path: Path, envelope: dict[str, Any]) -> None:
    data = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
    write_bytes_atomic(path, data, mode=0o600, suppress_cleanup_error=True)


def _read_json(path: Path) -> dict[str, Any]:
    size_bytes = path.stat().st_size
    if size_bytes > MAX_ENVELOPE_BYTES:
        raise ValueError(
            f"exec envelope {path} is {size_bytes} bytes, over the {MAX_ENVELOPE_BYTES} "
            "ceiling — this state snapshot or result delta is over the exec envelope limit; "
            "compact the conversation"
        )
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not _is_object_dict(data):
        raise TypeError(f"exec envelope {path} is {type(data).__name__}, expected object")
    return {str(k): v for k, v in data.items()}
