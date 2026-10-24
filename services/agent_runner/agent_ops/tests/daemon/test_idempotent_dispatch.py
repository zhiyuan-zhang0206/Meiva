# meiva:note
# Keyed operations: claim, execute once, replay, and never re-execute an uncertain claim.

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest

from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_ops.idempotency_plan import request_hash
from services.agent_runner.agent_ops.tests.ops_support import (
    Ops,
    config_write,
    executions,
    held_env_lock,
)


def _stored(conn: psycopg.Connection, key: str) -> tuple[object, ...]:
    row = conn.execute(
        "SELECT method, path, op_status, response_body, request_hash FROM api_idempotency "
        "WHERE key = %s",
        (key,),
    ).fetchone()
    assert row is not None
    return row


async def test_the_first_run_executes_once_and_stores_its_identity_and_result(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    payload = config_write("first-run")

    status, result = await ops.idempotent("config_write", payload, "key-1")

    assert status is OpStatus.COMPLETED
    assert executions("first-run") == 1
    assert _stored(db_conn, "key-1") == (
        "ops",
        "config_write",
        "completed",
        result,
        request_hash("config_write", payload),
    )


async def test_a_replay_answers_the_stored_result_without_executing_again(ops: Ops) -> None:
    payload = config_write("replayed")

    first = await ops.idempotent("config_write", payload, "key-2")
    second = await ops.idempotent("config_write", payload, "key-2")

    assert first == second
    assert executions("replayed") == 1


async def test_the_same_request_in_another_key_order_is_a_replay(ops: Ops) -> None:
    first = await ops.idempotent("config_write", config_write("reordered"), "key-3")
    reordered = {"trace_id": "reordered", "overrides": {"ops_concurrency": 3}}

    second = await ops.idempotent("config_write", reordered, "key-3")

    assert second == first
    assert executions("reordered") == 1


async def test_distinct_keys_execute_twice(ops: Ops) -> None:
    payload = config_write("two-keys")

    await ops.idempotent("config_write", payload, "key-a")
    await ops.idempotent("config_write", payload, "key-b")

    assert executions("two-keys") == 2


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("config_write", config_write("conflict", value=4)),
        ("config_read", {}),
    ],
)
async def test_a_key_reused_for_another_request_fails_without_executing(
    ops: Ops, db_conn: psycopg.Connection, kind: str, payload: dict[str, object]
) -> None:
    await ops.idempotent("config_write", config_write("conflict", value=3), "intent")

    status, result = await ops.idempotent(kind, payload, "intent")

    assert status is OpStatus.FAILED
    assert "identity conflict" in str(result["error"])
    assert executions("conflict") == 1
    assert _stored(db_conn, "intent")[:3] == ("ops", "config_write", "completed")


async def test_a_failed_outcome_is_stored_and_replayed(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    payload: dict[str, object] = {"path": "/garbage", "body": {}}

    first = await ops.idempotent("lifecycle", payload, "key-failed")
    second = await ops.idempotent("lifecycle", payload, "key-failed")

    assert first[0] is OpStatus.FAILED
    assert second == first
    assert _stored(db_conn, "key-failed")[2] == "failed"


async def test_an_unknown_kind_fails_before_claiming_the_key(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    status, result = await ops.idempotent("bogus_kind", {}, "key-bogus")

    assert status is OpStatus.FAILED
    assert "unknown kind" in str(result["error"])
    assert db_conn.execute(
        "SELECT count(*) FROM api_idempotency WHERE key = 'key-bogus'"
    ).fetchone() == (0,)


async def test_a_waiter_gets_the_result_of_a_slow_running_owner(ops: Ops) -> None:
    payload = config_write("slow-owner")

    with held_env_lock():
        owner = asyncio.create_task(ops.idempotent("config_write", payload, "key-slow"))
        await _until(lambda: "config_write" in ops.active_ops)
        waiter = asyncio.create_task(
            ops.idempotent(
                "config_write", payload, "key-slow", dedup_wait_attempts=400, dedup_wait_step_s=0.02
            )
        )
        await asyncio.sleep(0.2)
        assert not waiter.done(), "the waiter returned while the owner was still running"

    assert await waiter == await owner
    assert executions("slow-owner") == 1


async def test_a_waiter_fails_after_its_bounded_wait_and_never_executes(ops: Ops) -> None:
    payload = config_write("bounded-wait")

    with held_env_lock():
        owner = asyncio.create_task(ops.idempotent("config_write", payload, "key-bounded"))
        await _until(lambda: "config_write" in ops.active_ops)

        status, result = await ops.idempotent(
            "config_write", payload, "key-bounded", dedup_wait_attempts=3, dedup_wait_step_s=0.01
        )

        assert status is OpStatus.FAILED
        assert "outcome uncertain" in str(result["error"])
    await owner
    assert executions("bounded-wait") == 1


async def test_cancelling_the_owner_never_frees_its_key(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    payload = config_write("cancelled-owner")

    with held_env_lock():
        owner = asyncio.create_task(ops.idempotent("config_write", payload, "key-cancel"))
        await _until(lambda: "config_write" in ops.active_ops)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    await _until(lambda: executions("cancelled-owner") == 1)

    status, result = await ops.idempotent("config_write", payload, "key-cancel")

    assert status is OpStatus.FAILED
    assert "outcome uncertain" in str(result["error"])
    assert executions("cancelled-owner") == 1
    assert _stored(db_conn, "key-cancel")[2] is None


async def test_a_failed_result_write_keeps_the_claim_without_re_executing(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    payload = config_write("result-write-fails")
    db_conn.execute(
        "ALTER TABLE api_idempotency ADD CONSTRAINT reject_test_result "
        "CHECK (key <> 'intent' OR op_status IS NULL)"
    )
    db_conn.commit()
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            await ops.idempotent("config_write", payload, "intent", pass_only=True)
    finally:
        db_conn.execute("ALTER TABLE api_idempotency DROP CONSTRAINT reject_test_result")
        db_conn.commit()

    status, result = await ops.idempotent("config_write", payload, "intent", pass_only=True)

    assert status is OpStatus.FAILED
    assert "outcome uncertain" in str(result["error"])
    assert executions("result-write-fails") == 1
    assert _stored(db_conn, "intent")[2] is None


async def test_an_old_receipt_does_not_expire_into_a_fresh_execution(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    payload = config_write("old-receipt")
    first = await ops.idempotent("config_write", payload, "intent")
    db_conn.execute("UPDATE api_idempotency SET completed_at = now() - interval '8 days'")
    db_conn.commit()

    second = await ops.idempotent("config_write", payload, "intent")

    assert second == first
    assert executions("old-receipt") == 1


async def test_a_row_without_a_recorded_identity_fails_closed(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    db_conn.execute(
        "INSERT INTO api_idempotency(key, method, path, op_status, response_body) "
        "VALUES ('intent', 'ops', 'config_write', 'completed', '{}')"
    )
    db_conn.commit()

    status, result = await ops.idempotent("config_write", config_write("legacy"), "intent")

    assert status is OpStatus.FAILED
    assert "legacy identity unavailable" in str(result["error"])
    assert executions("legacy") == 0


def _record(conn: psycopg.Connection, status: str | None) -> None:
    conn.execute(
        "INSERT INTO api_idempotency(key, method, path, op_status, response_body, completed_at, "
        "request_hash) VALUES ('status-test', 'ops', 'config_read', %s, '{}', now(), %s)",
        (status, hashlib.sha256(b'["config_read",{}]').hexdigest()),
    )
    conn.commit()


@pytest.mark.parametrize("status", list(OpStatus))
async def test_a_replay_answers_the_canonical_stored_status(
    ops: Ops, db_conn: psycopg.Connection, status: OpStatus
) -> None:
    _record(db_conn, status.value)

    actual, result = await ops.idempotent("config_read", {}, "status-test", pass_only=True)

    assert actual is status
    assert result == {}


@pytest.mark.parametrize("status", ["pending", "", "enqueued"])
async def test_a_stored_status_outside_the_vocabulary_is_never_replayed(
    ops: Ops, db_conn: psycopg.Connection, status: str
) -> None:
    _record(db_conn, status)

    with pytest.raises(ValueError, match="OpStatus"):
        await ops.idempotent("config_read", {}, "status-test", pass_only=True)

    assert db_conn.execute(
        "SELECT op_status FROM api_idempotency WHERE key = 'status-test'"
    ).fetchone() == (status,)


async def test_a_null_status_still_waits_for_its_owner(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    _record(db_conn, None)

    status, result = await ops.idempotent("config_read", {}, "status-test", pass_only=True)

    assert status is OpStatus.FAILED
    assert "never completed" in str(result["error"])


def test_the_request_identity_migration_upgrades_and_replays(db_conn: psycopg.Connection) -> None:
    migration = Path(__file__).resolve().parents[5] / (
        "migrations/2026/10/07/10/20261007T103915_ops-request-identity.sql"
    )
    with db_conn.transaction():
        db_conn.execute("ALTER TABLE api_idempotency DROP COLUMN request_hash")
        db_conn.execute(cast(LiteralString, migration.read_text()))
        db_conn.execute(cast(LiteralString, migration.read_text()))
        row = db_conn.execute(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name='api_idempotency' AND column_name='request_hash'"
        ).fetchone()
        assert row == ("text", "YES")


async def _until(condition: Callable[[], bool], *, timeout_s: float = 10.0) -> None:
    for _ in range(int(timeout_s / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")
