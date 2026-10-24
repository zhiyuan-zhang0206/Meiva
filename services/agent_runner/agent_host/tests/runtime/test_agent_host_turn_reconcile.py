from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool, PoolClosed

from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime
from agent.ownership.tests.ownership_support import admit_owner, agent_row
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host import settlement as settlement_mod


async def _claimed_chat(
    conn: psycopg.Connection, pool: AsyncConnectionPool, owner: RuntimeIncarnation
) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, 'work the turn finished', 'chat', 'user') RETURNING id",
        (owner.agent_id,),
    ).fetchone()
    conn.commit()
    assert row is not None
    assert [c.id for c in await claim_inbound_batch(pool, owner.agent_id, incarnation=owner)] == [
        row[0]
    ]
    return row[0]


def _inbound(conn: psycopg.Connection, inbound_id: int) -> tuple[str, dict[str, Any] | None]:
    row = conn.execute(
        "SELECT status, payload FROM inbound_messages WHERE id=%s", (inbound_id,)
    ).fetchone()
    assert row is not None
    return row[0], row[1]


def _skips(loguru_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in loguru_records if r["extra"].get("event") == "host_turn_reconcile_skipped"]


async def test_pass_consumes_the_finished_turns_claims(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    owner = await admit_owner(aops_pool, agent_row(db_conn), database_gate=database_gate)
    claimed = await _claimed_chat(db_conn, aops_pool, owner)

    await settlement_mod.reconcile_inbounds_after_turn(aops_pool, owner)

    status, payload = _inbound(db_conn, claimed)
    assert status == "done"
    assert payload is None, "a finished turn's claims are consumed as ordinary work"


async def test_replaced_runtime_is_a_logged_noop(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    loguru_records: list[dict[str, Any]],
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await admit_owner(aops_pool, agent_row(db_conn), database_gate=database_gate)
    claimed = await _claimed_chat(db_conn, aops_pool, owner)
    replacement = await admit_hosted_runtime(
        aops_pool, owner.agent_id, "claim-test", uuid4(), db=database
    )
    assert replacement is not None and replacement.generation != owner.generation

    await settlement_mod.reconcile_inbounds_after_turn(aops_pool, owner)

    skips = _skips(loguru_records)
    assert [r["extra"]["reason"] for r in skips] == ["ownership_lost"]
    assert [r["level"].name for r in skips] == ["INFO"]
    assert _inbound(db_conn, claimed)[0] == "claimed", "the replacement disposes the claim"


async def test_unexpected_failure_is_logged_not_raised(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: list[dict[str, Any]],
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await admit_owner(aops_pool, agent_row(db_conn), database_gate=database_gate)
    await aops_pool.close()

    await settlement_mod.reconcile_inbounds_after_turn(aops_pool, owner)

    failed = [r for r in loguru_records if r["extra"].get("event") == "host_turn_reconcile_failed"]
    assert [r["extra"]["agent_id"] for r in failed] == [owner.agent_id]
    assert failed[0]["exception"] is not None
    assert failed[0]["exception"].type is PoolClosed
