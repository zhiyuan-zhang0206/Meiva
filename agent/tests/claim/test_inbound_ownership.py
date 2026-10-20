import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import (
    claim_inbound_batch,
)
from agent.ownership.inbound import RuntimeOwnershipLostError, lock_inbound_owner
from agent.ownership.tests.ownership_support import admit_owner, agent_row, insert
from base.db import create_agent
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation


@pytest.mark.parametrize("kind", ["restart", "terminate"])
async def test_unowned_lifecycle_refuses_before_acknowledging_any_batch(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, kind: str
) -> None:
    agent_id = agent_row(db_conn)
    chat = insert(db_conn, agent_id)
    command = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'',%s,'user') RETURNING id",
        (agent_id, kind),
    ).fetchone()
    assert command is not None
    db_conn.commit()
    with pytest.raises(RuntimeError, match="lifecycle claim requires an admitted"):
        await claim_inbound_batch(aops_pool, agent_id, incarnation=None)
    assert db_conn.execute(
        "SELECT id,status,claimed_at FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall() == [(chat, "pending", None), (command[0], "pending", None)]


async def test_unowned_ordinary_batch_remains_compatible(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = agent_row(db_conn)
    insert(db_conn, agent_id)
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'summary','compact_summary','user')",
        (agent_id,),
    )
    db_conn.commit()
    assert [
        item.kind for item in await claim_inbound_batch(aops_pool, agent_id, incarnation=None)
    ] == [
        "chat",
        "compact_summary",
    ]
    assert db_conn.execute(
        "SELECT kind,status FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent_id,)
    ).fetchall() == [("chat", "claimed"), ("compact_summary", "done")]


async def test_successful_claim_clears_wake_suppression(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = agent_row(db_conn)
    inbound = insert(db_conn, agent_id)
    db_conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until=now()+interval '1 hour', "
        "wake_suppress_reason='resurrect_failed' WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()

    assert [row.id for row in await claim_inbound_batch(aops_pool, agent_id, incarnation=None)] == [
        inbound
    ]
    assert db_conn.execute(
        "SELECT wake_suppressed_until,wake_suppress_reason FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == (None, None)


@pytest.mark.parametrize("missing", [False, True])
async def test_stale_or_missing_owner_cannot_claim(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    missing: bool,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    inbound = insert(db_conn, agent_id)
    stale = None if missing else RuntimeIncarnation(agent_id, owner.generation, uuid4())
    with pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, agent_id, incarnation=stale)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("pending",)


async def test_missing_runtime_metadata_cannot_claim(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent_id = create_agent(db_conn)
    inbound = insert(db_conn, agent_id)
    with pytest.raises(RuntimeOwnershipLostError):
        await claim_inbound_batch(aops_pool, agent_id, incarnation=None)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound,)
    ).fetchone() == ("pending",)


async def test_two_owners_compete_only_current_owner_claims(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    inbound = insert(db_conn, agent_id)

    async def claim(token: RuntimeIncarnation) -> list[int]:
        try:
            return [
                row.id for row in await claim_inbound_batch(aops_pool, agent_id, incarnation=token)
            ]
        except RuntimeOwnershipLostError:
            return []

    stale = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    assert await asyncio.gather(claim(stale), claim(owner)) == [[], [inbound]]


async def test_owner_locked_queue_mutation_rolls_back(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database_gate: ProcessDbGate
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    inbound = insert(db_conn, agent_id)
    with pytest.raises(RuntimeError, match="injected rollback"):
        async with async_write_transaction(aops_pool) as conn:
            await lock_inbound_owner(conn, agent_id, incarnation=owner)
            await conn.execute(
                "UPDATE inbound_messages SET status='claimed' WHERE id=%s", (inbound,)
            )
            raise RuntimeError("injected rollback")
    assert [r.id for r in await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)] == [
        inbound
    ]
