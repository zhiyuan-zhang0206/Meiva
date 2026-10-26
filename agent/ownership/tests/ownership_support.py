import json
from collections.abc import AsyncGenerator
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest_asyncio
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from redis.asyncio.client import PubSub

from agent.ownership.hosted import admit_hosted_runtime
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation


# meiva:note
# A bus whose publication raises `failure`: a programming defect, which a real Redis cannot
# produce (it recovers every transport failure itself).
class _PublicationDefectBus:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    async def publish_best_effort(
        self, payload: str, *, channel: str | None = None, context: str = ""
    ) -> int | None:
        raise self.failure


# meiva:note
# Constructor entry for `_PublicationDefectBus` typed as `EventBus` so tests pass a failing bus
# without subclassing `EventBus`.
def PublicationDefectBus(failure: BaseException) -> EventBus:  # noqa: N802
    return cast(EventBus, _PublicationDefectBus(failure))


def agent_row(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id,status,machine) VALUES (%s,'idling','claim-test') "
        "ON CONFLICT (id) DO UPDATE SET status='idling',machine='claim-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def insert(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, 'durable', 'chat', 'user') RETURNING id",
        (agent_id,),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def insert_lifecycle_command(
    conn: psycopg.Connection, agent_id: int, kind: str, payload: dict[str, object] | None = None
) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
        "VALUES (%s,'',%s,'user',%s) RETURNING id",
        (agent_id, kind, Jsonb(payload) if payload is not None else None),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


async def admit_owner(
    pool: AsyncConnectionPool, agent_id: int, database_gate: ProcessDbGate
) -> RuntimeIncarnation:
    owner = await admit_hosted_runtime(
        pool,
        agent_id,
        "claim-test",
        uuid4(),
        db=Database.from_settings(gate=database_gate),
    )
    assert owner is not None
    return owner


# meiva:note
# A real subscriber on the event bus's channel: the hints a test's code published, read back
# from Redis instead of from a recording double.
class BusTap:
    def __init__(self, subscription: PubSub) -> None:
        self._subscription = subscription

    async def agent_updated_ids(self) -> list[int]:
        # meiva:note
        # The agent ids of the change hints that arrived since the previous call, in arrival
        # order. The redis stubs leave `get_message` untyped.
        ids: list[int] = []
        while True:
            message: dict[str, Any] | None = await self._subscription.get_message(  # pyright: ignore[reportUnknownMemberType]
                ignore_subscribe_messages=True, timeout=0.3
            )
            if message is None:
                return ids
            event: dict[str, Any] = json.loads(message["data"])
            if event["role"] == "agent_updated":
                ids.append(event["agent_id"])


# meiva:note
# The tap subscribes before the test publishes anything. The redis stubs leave `pubsub`
# and `get_message` untyped.
@pytest_asyncio.fixture
async def bus_tap(event_bus: EventBus) -> AsyncGenerator[BusTap]:
    client = event_bus.open_async_redis()
    async with client.pubsub() as subscription:  # pyright: ignore[reportUnknownMemberType]
        await subscription.subscribe(event_bus.channel)
        await subscription.get_message(timeout=1.0)  # pyright: ignore[reportUnknownMemberType]
        yield BusTap(subscription)
    await client.aclose()
