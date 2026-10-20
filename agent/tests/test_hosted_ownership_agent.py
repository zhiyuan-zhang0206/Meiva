from unittest.mock import ANY, AsyncMock
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership import hosted, hosted_cleanup
from agent.ownership.hosted import (
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    settle_hosted_runtime,
)
from base.agents.inbox.insert import insert_inbound_message
from base.db import Database, create_agent
from base.events.live.bus import EventBus


def _agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', 'host-test') "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = 'host-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


async def test_hosted_status_changes_publish_agent_updated(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    publish = AsyncMock()
    monkeypatch.setattr(hosted, "publish_agent_updated", publish)
    monkeypatch.setattr(hosted_cleanup, "publish_agent_updated", publish)
    agent_id, owner = _agent(db_conn), uuid4()

    incarnation = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert incarnation is not None
    publish.assert_not_awaited()

    publish.reset_mock()
    assert await settle_hosted_runtime(
        aops_pool,
        incarnation,
        bus=event_bus,
    )
    publish.assert_awaited_once_with(ANY, agent_id)

    incarnation = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert incarnation is not None
    insert_inbound_message(
        db_conn, agent_id, "", "user", "terminate", bus=event_bus, database=database
    )
    publish.reset_mock()
    await claim_inbound_batch(aops_pool, agent_id, incarnation=incarnation)
    assert (
        await apply_hosted_lifecycle(
            aops_pool,
            incarnation,
            bus=event_bus,
            resume_turn=False,
        )
        == "terminate"
    )
    publish.assert_awaited_once_with(ANY, agent_id)


async def test_hosted_restart_releases_before_new_incarnation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    first = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert first is not None
    insert_inbound_message(
        db_conn, agent_id, "", "user", "restart", bus=event_bus, database=database
    )
    await claim_inbound_batch(aops_pool, agent_id, incarnation=first)
    assert (
        await apply_hosted_lifecycle(
            aops_pool,
            first,
            bus=event_bus,
            resume_turn=False,
        )
        == "restart"
    )
    second = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert second is not None and second.generation != first.generation
    assert not await settle_hosted_runtime(
        aops_pool,
        first,
        bus=event_bus,
    )
