"""Immutable PATCH task results committed by the task mutation transaction."""

from dataclasses import dataclass

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from base.db.rows import query_one
from gateway.schemas.tasks import TaskRow


# meiva:note
# A stored task patch: the request that was accepted and its result, both jsonb.
@dataclass(frozen=True)
class _StoredPatch:
    request: object
    result: object


def existing_task_receipt(
    conn: psycopg.Connection, path: str, key: str, body: dict[str, object]
) -> TaskRow | None:
    """Serialize an operation before touching mutable task state."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (path + ":" + key,))
    row = query_one(
        conn,
        _StoredPatch,
        "SELECT request, result FROM task_patch_receipts WHERE path=%s AND operation_key=%s",
        (path, key),
    )
    if row is None:
        return None
    if row.request != body:
        raise HTTPException(
            status_code=409, detail="idempotency key identifies a different task patch"
        )
    return TaskRow.model_validate(row.result)


def save_task_receipt(
    conn: psycopg.Connection, path: str, key: str, body: dict[str, object], task: TaskRow
) -> None:
    """Keep the original response without pinning task or inbound retention."""
    conn.execute(
        "INSERT INTO task_patch_receipts (path, operation_key, request, result) "
        "VALUES (%s, %s, %s, %s)",
        (path, key, Jsonb(body), Jsonb(task.model_dump(mode="json"))),
    )
