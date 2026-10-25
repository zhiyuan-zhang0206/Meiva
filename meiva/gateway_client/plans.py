# meiva:note
# The gateway requests the SDK sends, as values. Each function takes the facts a request is built
# from (the caller's identity as a string, a minted key, the validated arguments) and returns a
# `Request`: the method, path, body, the timeout and retry choice and the idempotency key. The
# facade in `meiva/gateway_client/__init__.py` reads the process once (the actor, a fresh key, the
# bound context), asks this module for the plan and runs it through the base transport, so a test
# asserts the plan or the request that really went out and never replaces the transport. A
# `None` timeout or retry count means "the context's own configured value".
# meiva:link meiva/agents/docs/agents.meiva.okf.md
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeGuard

from base.api_contracts.idempotency import validate_idempotency_key


def _is_str_dict(val: object) -> TypeGuard[dict[str, object]]:
    return isinstance(val, dict)


__all__ = [
    "SEND_TIMEOUT_SECONDS",
    "NoteIntent",
    "Request",
    "SpawnIntent",
    "SpawnReceipt",
    "list_agents_request",
    "message_request",
    "patch_label_request",
    "spawn_receipt",
    "spawn_request",
    "system_note_request",
]

SEND_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class Request:
    method: Literal["GET", "POST", "PATCH"]
    path: str
    body: dict[str, Any] | None = None
    params: dict[str, str | int] | None = None
    timeout_seconds: float | None = None
    idempotency_key: str | None = None
    max_retries: int | None = None


@dataclass(frozen=True, slots=True)
class SpawnIntent:
    spawner: str
    prompt: str | None
    fork_from: int | None
    prompt_source: str
    machine: str | None
    config: dict[str, object] | None
    label: str | None
    harness: str | None


@dataclass(frozen=True, slots=True)
class NoteIntent:
    content: str
    note_tag: str
    source: str
    task_id: int | None
    resurrect: bool


@dataclass(frozen=True, slots=True)
class SpawnReceipt:
    agent_id: int
    withdrawn_model: tuple[object, object] | None


def spawn_request(intent: SpawnIntent, idempotency_key: str) -> Request:
    key = validate_idempotency_key(idempotency_key)
    body: dict[str, Any] = {"spawner": intent.spawner}
    if intent.prompt is not None:
        body["prompt"] = intent.prompt
        body["prompt_source"] = intent.prompt_source
    if intent.fork_from is not None:
        body["fork_from"] = intent.fork_from
    if intent.machine is not None:
        body["machine"] = intent.machine
    if intent.config is not None:
        body["config"] = intent.config
    if intent.label is not None:
        body["label"] = intent.label
    if intent.harness is not None:
        body["harness"] = intent.harness
    return Request("POST", "/api/keyed/v1/agents", body=body, idempotency_key=key)


def spawn_receipt(data: Mapping[str, object]) -> SpawnReceipt:
    raw_id = data["id"]
    if not isinstance(raw_id, (int, str)) or isinstance(raw_id, bool):
        raise TypeError(f"spawn id must be int or str, got {type(raw_id).__name__}")
    agent_id = int(raw_id)
    normalized = data.get("config_normalized")
    if not _is_str_dict(normalized) or not normalized:
        return SpawnReceipt(agent_id, None)
    return SpawnReceipt(agent_id, (normalized.get("requested"), normalized.get("resolved")))


def message_request(
    agent_id: int,
    *,
    content: str | list[dict[str, object]],
    source: str,
    completion_notice: bool,
    idempotency_key: str,
) -> Request:
    body: dict[str, Any] = {"content": content, "source": source}
    if completion_notice:
        body["completion_notice"] = True
    return Request(
        "POST",
        f"/api/agents/{agent_id}/messages",
        body=body,
        timeout_seconds=SEND_TIMEOUT_SECONDS,
        idempotency_key=idempotency_key,
    )


def system_note_request(agent_id: int, note: NoteIntent, idempotency_key: str) -> Request:
    key = validate_idempotency_key(idempotency_key)
    body: dict[str, str | bool | int] = {
        "content": note.content,
        "note_tag": note.note_tag,
        "source": note.source,
        "resurrect": note.resurrect,
    }
    if note.task_id is not None:
        body["task_id"] = note.task_id
    return Request(
        "POST",
        f"/api/agents/{agent_id}/system-note",
        body=body,
        timeout_seconds=SEND_TIMEOUT_SECONDS,
        idempotency_key=key,
    )


def list_agents_request(*, scope: str, query: str, before_id: int | None, limit: int) -> Request:
    params: dict[str, str | int] = {"scope": scope, "query": query, "limit": limit}
    if before_id is not None:
        params["before_id"] = before_id
    return Request("GET", "/api/agents", params=params)


def patch_label_request(agent_id: int, label: str, source: str) -> Request:
    return Request("PATCH", f"/api/agents/{agent_id}", body={"label": label, "source": source})
