"""Retained compound acceptance tombstones; no FK or mutable-state replay."""

from dataclasses import dataclass

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from base.agents.tasks.model import validate_task_snapshot
from gateway.agents.task_assignment.schemas import TaskAssignmentResult
from gateway.http.db_rows import query_one
from gateway.http.json_values import is_object_dict


# meiva:note
# The stored pair of a receipt: the request that was accepted and the immutable result, both jsonb.
@dataclass(frozen=True)
class _ReceiptRow:
    request: object
    result: object


# meiva:note
# The birth metadata of the accepted agent, built in SQL as one jsonb value.
@dataclass(frozen=True)
class _BirthSnapshotRow:
    snapshot: object


def validate_result(value: object) -> TaskAssignmentResult:
    """Validate complete native Task snapshots before dataclass defaults can apply."""
    if not is_object_dict(value):
        raise TypeError("stored task assignment result is not an object")
    task = value["task"]
    if not is_object_dict(task):
        raise TypeError("stored task assignment snapshot is not an object")
    return TaskAssignmentResult.model_validate({**value, "task": validate_task_snapshot(task)})


def existing_assignment(
    conn: psycopg.Connection, key: str, request: dict[str, object]
) -> TaskAssignmentResult | None:
    """Wait for the logical operation before inspecting its immutable result."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
    row = query_one(
        conn,
        _ReceiptRow,
        "SELECT request,result FROM task_assignment_receipts WHERE operation_key=%s",
        (key,),
    )
    if row is None:
        return None
    if row.request != request:
        raise HTTPException(
            status_code=409, detail="idempotency key identifies a different task assignment"
        )
    return validate_result(row.result)


def save_assignment(
    conn: psycopg.Connection, key: str, request: dict[str, object], result: TaskAssignmentResult
) -> None:
    """Commit the original pair with birth, task, audit and durable assignment."""
    row = query_one(
        conn,
        _BirthSnapshotRow,
        "SELECT jsonb_build_object('machine',machine,'config_overlay',config_overlay,"
        "'birth_config',birth_config,'preset_name',NULL,"
        "'launch_attempt_id',last_launch_attempt_id) AS snapshot FROM agents_meta WHERE id=%s",
        (result.agent_id,),
    )
    if row is None:
        raise RuntimeError("accepted birth metadata is missing")
    conn.execute(
        "INSERT INTO task_assignment_receipts(operation_key,request,result,birth_snapshot) "
        "VALUES (%s,%s,%s,%s)",
        (key, Jsonb(request), Jsonb(result.model_dump(mode="json")), Jsonb(row.snapshot)),
    )
