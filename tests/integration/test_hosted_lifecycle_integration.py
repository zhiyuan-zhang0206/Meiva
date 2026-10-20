import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from agent.ownership.tests.ownership_support import admit_owner, agent_row, insert_lifecycle_command
from base.config import settings
from base.db import PG_KEEPALIVE_KWARGS, Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from ops.lifecycle.termination import force_terminate_transaction


@pytest.mark.parametrize("executed", [False, True])
async def test_hosted_force_cannot_be_undone_by_prior_restart(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    executed: bool,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    first = insert_lifecycle_command(db_conn, agent_id, "restart")
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    if executed:
        assert (
            await apply_hosted_lifecycle(
                aops_pool,
                owner,
                bus=event_bus,
                resume_turn=False,
            )
            == "restart"
        )
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        _, _, force, _cutoff = await asyncio.to_thread(
            force_terminate_transaction, agent_id, pool, source="user"
        )
    later = insert_lifecycle_command(db_conn, agent_id, "restart")
    assert (
        await apply_hosted_lifecycle(
            aops_pool,
            owner,
            bus=event_bus,
            resume_turn=False,
        )
        is None
    )
    assert (
        await admit_hosted_runtime(aops_pool, agent_id, "claim-test", uuid4(), db=database) is None
    )
    assert db_conn.execute(
        "SELECT status,payload->'lifecycle_result'->>'reason' FROM inbound_messages WHERE id=%s",
        (first,),
    ).fetchone() == ("done", None if executed else "force_terminate")
    assert db_conn.execute(
        "SELECT id,status FROM inbound_messages WHERE id IN (%s,%s) ORDER BY id", (force, later)
    ).fetchall() == [(force, "done"), (later, "pending")]
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated",)
