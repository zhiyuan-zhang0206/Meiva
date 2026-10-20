import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership import corpse_reap
from agent.ownership.corpse_reap import reap_crash_corpses
from agent.ownership.hosted import (
    admit_hosted_runtime,
    settle_hosted_runtime,
    settle_running_rows_at_start,
    stamp_turn_fatal,
)
from base.cluster.machine import MachineIdentity
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry import Event


def _agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', 'host-test') "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = 'host-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


async def test_hosted_incarnation_survives_idle_and_next_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    first = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert first is not None
    assert await settle_hosted_runtime(
        aops_pool,
        first,
        bus=event_bus,
    )
    second = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert second == first


@pytest.mark.parametrize("status", ["running", "idling"])
async def test_another_owner_takes_over_and_fences_the_old_settlement(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    status: str,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = _agent(db_conn)
    old = await admit_hosted_runtime(aops_pool, agent_id, "host-test", uuid4(), db=database)
    assert old is not None
    if status == "idling":
        assert await settle_hosted_runtime(aops_pool, old, bus=event_bus)
    new = await admit_hosted_runtime(aops_pool, agent_id, "host-test", uuid4(), db=database)
    assert new is not None and new.generation != old.generation
    assert not await settle_hosted_runtime(aops_pool, old, bus=event_bus)


async def test_admission_refuses_only_terminated_and_other_machine_rows(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
) -> None:
    terminated, elsewhere = _agent(db_conn), _agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (terminated,))
    db_conn.execute("UPDATE agents_meta SET machine='other-host' WHERE id=%s", (elsewhere,))
    db_conn.commit()
    for agent_id in (terminated, elsewhere):
        assert (
            await admit_hosted_runtime(aops_pool, agent_id, "host-test", uuid4(), db=database)
            is None
        )
    assert db_conn.execute(
        "SELECT status, machine, runtime_owner FROM agents_meta WHERE id=%s", (elsewhere,)
    ).fetchone() == ("idling", "other-host", None)


async def test_admission_is_one_update_that_stamps_the_row(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert incarnation is not None
    assert db_conn.execute(
        "SELECT status, runtime_owner, runtime_generation, last_admission_at IS NOT NULL "
        "FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == ("running", owner, incarnation.generation, True)


async def test_admission_does_not_lock_deployment_state(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    try:
        db_conn.execute("SELECT id FROM deployment_state WHERE id = 1 FOR UPDATE")
        admitted = await asyncio.wait_for(
            admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database),
            10,
        )
        assert admitted is not None
    finally:
        db_conn.rollback()


def _marker(db_conn: psycopg.Connection, agent_id: int) -> object:
    row = db_conn.execute(
        "SELECT last_turn_fatal_at FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert row is not None, f"agents_meta row {agent_id} missing"
    return row[0]


def _set_marker(
    db_conn: psycopg.Connection, agent_id: int, *, minutes_ago: int | None = None
) -> None:
    if minutes_ago is None:
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = NULL WHERE id = %s", (agent_id,)
        )
    else:
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = now() - make_interval(mins => %s) "
            "WHERE id = %s",
            (minutes_ago, agent_id),
        )
    db_conn.commit()


async def test_stamp_turn_fatal_is_monotonic_and_cas_guarded(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert incarnation is not None

    assert (await stamp_turn_fatal(aops_pool, incarnation)).applied
    first = _marker(db_conn, agent_id)
    assert first is not None

    _set_marker(db_conn, agent_id, minutes_ago=100)
    old_stamp = _marker(db_conn, agent_id)
    assert (await stamp_turn_fatal(aops_pool, incarnation)).applied
    assert _marker(db_conn, agent_id) == old_stamp

    assert await settle_hosted_runtime(
        aops_pool,
        incarnation,
        bus=event_bus,
    )
    assert not (await stamp_turn_fatal(aops_pool, incarnation)).applied

    foreign = RuntimeIncarnation(agent_id, uuid4(), owner)
    assert not (await stamp_turn_fatal(aops_pool, foreign)).applied


async def test_settle_never_touches_the_corpse_marker(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert incarnation is not None

    _set_marker(db_conn, agent_id, minutes_ago=30)
    assert await settle_hosted_runtime(
        aops_pool,
        incarnation,
        bus=event_bus,
    )
    assert _marker(db_conn, agent_id) is not None

    assert await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    _set_marker(db_conn, agent_id, minutes_ago=None)
    assert await settle_hosted_runtime(
        aops_pool,
        incarnation,
        bus=event_bus,
    )
    assert _marker(db_conn, agent_id) is None


async def test_reap_crash_corpses_terminates_only_grace_elapsed_idling_corpses(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    event_bus: EventBus,
) -> None:
    events: list[tuple[int, str, str]] = []

    async def _event(_conn: object, event: Event) -> Event:
        if event.attributes.get("reason") == "corpse_reaper":
            assert event.agent_id is not None
            events.append((event.agent_id, event.event_name, "corpse_reaper"))
        return event

    monkeypatch.setattr(corpse_reap, "record_audit_async", _event)
    published: list[int] = []

    async def _publish(_bus: object, agent_id: int) -> None:
        published.append(agent_id)

    monkeypatch.setattr(corpse_reap, "publish_agent_updated", _publish)
    owner = uuid4()

    async def _row(
        minutes_ago: int | None, *, status: str = "idling", row_owner: object = owner
    ) -> int:
        agent_id, _owner = _agent(db_conn), owner
        incarnation = await admit_hosted_runtime(
            aops_pool, agent_id, "host-test", _owner, db=database
        )
        assert incarnation is not None
        await settle_hosted_runtime(
            aops_pool,
            incarnation,
            bus=event_bus,
        )
        db_conn.execute(
            "UPDATE agents_meta SET status=%s, runtime_owner=%s WHERE id=%s",
            (status, row_owner, agent_id),
        )
        _set_marker(db_conn, agent_id, minutes_ago=minutes_ago)
        return agent_id

    past_grace = await _row(minutes_ago=16)
    within_grace = await _row(minutes_ago=5)
    healthy = await _row(minutes_ago=None)
    running_corpse = await _row(minutes_ago=16, status="running")
    other_owner_corpse = await _row(minutes_ago=16, row_owner=uuid4())
    elsewhere = await _row(minutes_ago=16)
    db_conn.execute("UPDATE agents_meta SET machine='other-host' WHERE id=%s", (elsewhere,))
    db_conn.commit()

    published.clear()
    reaped = await reap_crash_corpses(aops_pool, "host-test", bus=event_bus)
    assert sorted(corpse.agent_id for corpse in reaped) == sorted([past_grace, other_owner_corpse])

    for corpse in reaped:
        assert db_conn.execute(
            "SELECT status, termination_source FROM agents_meta WHERE id = %s",
            (corpse.agent_id,),
        ).fetchone() == ("terminated", "reaper")

    for survivor, expected in (
        (within_grace, "idling"),
        (healthy, "idling"),
        (running_corpse, "running"),
        (elsewhere, "idling"),
    ):
        assert db_conn.execute(
            "SELECT status FROM agents_meta WHERE id = %s", (survivor,)
        ).fetchone() == (expected,)

    assert sorted(events) == sorted(
        [
            (past_grace, "status_change", "corpse_reaper"),
            (other_owner_corpse, "status_change", "corpse_reaper"),
        ]
    )
    assert sorted(published) == sorted([past_grace, other_owner_corpse])
    assert await reap_crash_corpses(aops_pool, "host-test", bus=event_bus) == []


async def test_crash_pipeline_marker_survives_settle_and_reaper_terminates(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, db=database)
    assert incarnation is not None

    assert await stamp_turn_fatal(aops_pool, incarnation)
    assert await settle_hosted_runtime(
        aops_pool,
        incarnation,
        bus=event_bus,
    )
    assert _marker(db_conn, agent_id) is not None

    assert await reap_crash_corpses(aops_pool, "host-test", bus=event_bus) == []

    _set_marker(db_conn, agent_id, minutes_ago=16)
    reaped = await reap_crash_corpses(aops_pool, "host-test", bus=event_bus)
    assert [corpse.agent_id for corpse in reaped] == [agent_id]
    row = db_conn.execute(
        "SELECT status, termination_source FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "terminated" and row[1] == "reaper"


async def test_settle_running_rows_at_start_idles_only_this_machines_running_rows(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    mine, other_owner, elsewhere, parked = (_agent(db_conn) for _ in range(4))
    for agent_id, owner in ((mine, uuid4()), (other_owner, uuid4()), (elsewhere, uuid4())):
        db_conn.execute(
            "UPDATE agents_meta SET status='running', runtime_generation=%s, runtime_owner=%s "
            "WHERE id=%s",
            (uuid4(), owner, agent_id),
        )
    db_conn.execute("UPDATE agents_meta SET machine='other-host' WHERE id=%s", (elsewhere,))
    db_conn.commit()

    assert sorted(await settle_running_rows_at_start(aops_pool, "host-test")) == sorted(
        [mine, other_owner]
    )
    statuses = dict(
        db_conn.execute(
            "SELECT id, status FROM agents_meta WHERE id = ANY(%s)",
            ([mine, other_owner, elsewhere, parked],),
        ).fetchall()
    )
    assert statuses == {
        mine: "idling",
        other_owner: "idling",
        elsewhere: "running",
        parked: "idling",
    }


async def test_replacement_admission_submits_the_interrupted_lease_notice_once(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    # meiva:note
    # A restart is what ends the takeover, so the replacement host owes the notice:
    # the in-memory relay child does not survive it and there is no retry ledger. The
    # fake app server speaks the recorded thread's transport, so the assertion is
    # that the notice arrived rather than that a function was called.
    # meiva:link docs/conventions/agents/agent-impersonation-hosts.md
    import shutil
    import tempfile
    from pathlib import Path
    from uuid import uuid4

    from base.agents.impersonation.tests.test_host_transport import FakeAppServer

    agent_id = _agent(db_conn)
    local_machine = machine_identity.name()
    db_conn.execute("UPDATE agents_meta SET machine=%s WHERE id=%s", (local_machine, agent_id))
    db_conn.commit()
    old = await admit_hosted_runtime(aops_pool, agent_id, local_machine, uuid4(), db=database)
    assert old is not None
    lease_id = uuid4()
    thread_id = str(uuid4())
    directory = Path(tempfile.mkdtemp(prefix="cxown-", dir="/tmp"))
    socket = directory / "host.sock"
    app_server = FakeAppServer()
    app_server.start(socket)
    try:
        db_conn.execute(
            "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,ttl_seconds,"
            "expires_at,relay_provider,relay_thread_id,relay_codex_remote,"
            "accepted_generation,accepted_owner) "
            "VALUES(%s,%s,'external_agent:codex',%s,'active',3600,"
            "clock_timestamp()+interval '1 hour','codex',%s,%s,%s,%s)",
            (
                lease_id,
                agent_id,
                local_machine,
                thread_id,
                f"unix://{socket}",
                old.generation,
                old.owner,
            ),
        )
        db_conn.commit()
        successor = await admit_hosted_runtime(
            aops_pool, agent_id, local_machine, uuid4(), db=database
        )
    finally:
        app_server.stop()
        shutil.rmtree(directory, ignore_errors=True)
    assert successor is not None and successor.generation != old.generation
    row = db_conn.execute(
        "SELECT status,rejection_reason FROM agent_impersonations WHERE id=%s", (lease_id,)
    ).fetchone()
    assert row == ("expired", "aborted: native runtime restarted")
    turns = [message for message in app_server.received if message.get("method") == "turn/start"]
    assert len(turns) == 1
    payload = turns[0]["params"]
    assert payload["threadId"] == thread_id
    assert str(lease_id) in payload["input"][0]["text"]
