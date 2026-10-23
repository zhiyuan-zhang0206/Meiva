from typing import override
from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import (
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    settle_hosted_runtime,
)
from base.agents.inbox.insert import insert_inbound_message
from base.db import Database, create_agent
from base.events.live.bus import EventBus, EventBusConfig
from base.events.live.projection import AgentUpdated


class _RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__(EventBusConfig(redis_url="redis://127.0.0.1:1/0", events_channel="test"))
        self.updated_agents: list[int] = []

    @override
    async def publish_best_effort(
        self, payload: str, *, channel: str | None = None, context: str = ""
    ) -> int | None:
        if context == "agent_updated":
            self.updated_agents.append(AgentUpdated.model_validate_json(payload).agent_id)
        return 1


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
    database: Database,
    event_bus: EventBus,
) -> None:
    bus = _RecordingBus()
    agent_id, owner = _agent(db_conn), uuid4()

    incarnation = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert incarnation is not None
    assert bus.updated_agents == []

    assert await settle_hosted_runtime(
        aops_pool,
        incarnation,
        bus=bus,
    )
    assert bus.updated_agents == [agent_id]

    incarnation = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert incarnation is not None
    insert_inbound_message(
        db_conn, agent_id, "", "user", "terminate", bus=event_bus, database=database
    )
    bus.updated_agents.clear()
    await claim_inbound_batch(aops_pool, agent_id, incarnation=incarnation)
    assert (
        await apply_hosted_lifecycle(
            aops_pool,
            incarnation,
            bus=bus,
            resume_turn=False,
        )
        == "terminate"
    )
    assert bus.updated_agents == [agent_id]


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
