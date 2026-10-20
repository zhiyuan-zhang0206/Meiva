import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import admit_hosted_runtime, apply_hosted_lifecycle
from base.config import settings
from base.db import PG_KEEPALIVE_KWARGS, Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.agents.wake import resurrect_agent
from ops.lifecycle.termination import force_terminate_transaction


def _agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id,status,machine) VALUES (%s,'idling','claim-test') "
        "ON CONFLICT (id) DO UPDATE SET status='idling',machine='claim-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def _command(conn: psycopg.Connection, agent_id: int, kind: str) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES (%s,'',%s,'user') RETURNING id",
        (agent_id, kind),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _die(conn: psycopg.Connection, agent_id: int) -> None:
    conn.execute(
        "UPDATE agents_meta SET status='terminated', termination_source='user' WHERE id=%s",
        (agent_id,),
    )
    conn.commit()


async def _admit(pool: AsyncConnectionPool, agent_id: int, *, db: Database) -> RuntimeIncarnation:
    owner = await admit_hosted_runtime(
        pool, agent_id, "claim-test", uuid4(), expected_from="idling", db=db
    )
    assert owner is not None
    return owner


def _epoch(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='resurrect' "
        "ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return row[0]


def _command_row(conn: psycopg.Connection, command: int) -> tuple[object, ...]:
    row = conn.execute(
        "SELECT status,payload->'lifecycle_result' FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone()
    assert row is not None
    return row


async def test_resurrect_supersedes_the_pending_terminate_it_would_replay(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    await _admit(aops_pool, agent_id, db=database)
    stale = _command(db_conn, agent_id, "terminate")
    _die(db_conn, agent_id)

    resurrect_agent(
        database,
        event_bus,
        agent_id,
        resurrected_by="user",
    )

    epoch = _epoch(db_conn, agent_id)
    assert _command_row(db_conn, stale) == (
        "done",
        {"outcome": "superseded", "reason": "resurrect", "resurrect_inbound_id": epoch},
    )

    owner = await _admit(aops_pool, agent_id, db=database)
    batch = await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    assert [item.kind for item in batch] == ["resurrect"]
    assert (
        await apply_hosted_lifecycle(
            aops_pool,
            owner,
            bus=event_bus,
        )
        is None
    )
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("running",)


async def test_a_command_created_after_the_resurrect_still_applies(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    await _admit(aops_pool, agent_id, db=database)
    _command(db_conn, agent_id, "terminate")
    _die(db_conn, agent_id)
    resurrect_agent(
        database,
        event_bus,
        agent_id,
        resurrected_by="user",
    )
    owner = await _admit(aops_pool, agent_id, db=database)
    newer = _command(db_conn, agent_id, "terminate")
    batch = await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    assert [item.kind for item in batch] == ["terminate"]
    assert (
        await apply_hosted_lifecycle(
            aops_pool,
            owner,
            bus=event_bus,
        )
        == "terminate"
    )
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated",)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (newer,)
    ).fetchone() == ("done",)


async def test_a_claimed_but_unapplied_command_is_superseded_not_deferred(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    owner = await _admit(aops_pool, agent_id, db=database)
    command = _command(db_conn, agent_id, "terminate")
    claimed = await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    assert [item.id for item in claimed] == [command]
    _die(db_conn, agent_id)

    resurrect_agent(
        database,
        event_bus,
        agent_id,
        resurrected_by="user",
    )

    epoch = _epoch(db_conn, agent_id)
    assert _command_row(db_conn, command) == (
        "done",
        {"outcome": "superseded", "reason": "resurrect", "resurrect_inbound_id": epoch},
    )


async def test_force_on_unadmitted_row_cannot_create_a_hosted_successor(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    from base.api_contracts.agent_contract import ResurrectRefused

    agent_id = _agent(db_conn)
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        _, _, _, force, _cutoff = await asyncio.to_thread(
            force_terminate_transaction, agent_id, pool, source="user"
        )
    with pytest.raises(ResurrectRefused, match="runtime_cutover_required"):
        resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="user",
        )
    assert _command_row(db_conn, force) == ("done", None)
    assert db_conn.execute(
        "SELECT status,runtime_kind FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated", None)


async def test_reclaiming_after_a_resurrect_preserves_command_history(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    await _admit(aops_pool, agent_id, db=database)
    stale = db_conn.execute(
        "INSERT INTO inbound_messages (agent_id,content,kind,source,payload) "
        "VALUES (%s,'','terminate','user','{\"note\": \"drain\"}') RETURNING id",
        (agent_id,),
    ).fetchone()
    assert stale is not None
    db_conn.commit()
    _die(db_conn, agent_id)
    resurrect_agent(
        database,
        event_bus,
        agent_id,
        resurrected_by="user",
    )
    epoch = _epoch(db_conn, agent_id)
    settled = _command_row(db_conn, stale[0])

    owner = await _admit(aops_pool, agent_id, db=database)
    for _ in range(2):
        batch = await claim_inbound_batch(
            aops_pool, agent_id, lifecycle_only=True, incarnation=owner
        )
        assert batch == []
    assert _command_row(db_conn, stale[0]) == settled
    payload = db_conn.execute(
        "SELECT payload FROM inbound_messages WHERE id=%s", (stale[0],)
    ).fetchone()
    assert payload is not None
    assert payload[0]["note"] == "drain"
    assert payload[0]["lifecycle_result"] == {
        "outcome": "superseded",
        "reason": "resurrect",
        "resurrect_inbound_id": epoch,
    }
