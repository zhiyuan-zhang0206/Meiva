import asyncio

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.tests.ownership_support import (
    BusTap,
    PublicationDefectBus,
    admit_owner,
    agent_row,
    insert_lifecycle_command,
)
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.log.tests.log_records import LogRecords
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.lifecycle.shells import (
    apply_lifecycle_command,
    reap_and_announce_corpses,
)


async def _claimed_terminate(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    *,
    kill_shells: bool,
) -> RuntimeIncarnation:
    agent_id = agent_row(conn)
    owner = await admit_owner(pool, agent_id, database_gate=database_gate)
    insert_lifecycle_command(
        conn, agent_id, "terminate", {"kill_all_shell_sessions": True} if kill_shells else None
    )
    conn.execute("UPDATE agents_meta SET session_index=7 WHERE id=%s", (agent_id,))
    conn.commit()
    await claim_inbound_batch(pool, agent_id, incarnation=owner)
    return owner


def _shell_cleanup_attempts(records: LogRecords, agent_id: int) -> list[str]:
    return [m for m in records.messages() if f"below 7 of agent {agent_id}" in m]


def _browser_release_attempts(records: LogRecords, agent_id: int) -> list[str]:
    return [m for m in records.messages("DEBUG") if f"agent {agent_id} browser-page release" in m]


async def test_a_terminate_that_asked_for_shells_cleans_below_its_cutoff_after_the_commit(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    bus_tap: BusTap,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_terminate(db_conn, aops_pool, database_gate, kill_shells=True)
    await bus_tap.agent_updated_ids()

    kind = await apply_lifecycle_command(aops_pool, owner, bus=event_bus, resume_turn=False)

    assert kind == "terminate"
    assert await bus_tap.agent_updated_ids() == [owner.agent_id]
    assert len(_browser_release_attempts(loguru_records, owner.agent_id)) == 1
    assert len(_shell_cleanup_attempts(loguru_records, owner.agent_id)) == 1


async def test_a_terminate_that_did_not_ask_for_shells_leaves_them_alone(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_terminate(db_conn, aops_pool, database_gate, kill_shells=False)

    kind = await apply_lifecycle_command(aops_pool, owner, bus=event_bus, resume_turn=False)

    assert kind == "terminate"
    assert len(_browser_release_attempts(loguru_records, owner.agent_id)) == 1
    assert _shell_cleanup_attempts(loguru_records, owner.agent_id) == []


async def test_a_restart_owes_no_cleanup(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    insert_lifecycle_command(db_conn, agent_id, "restart")
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)

    kind = await apply_lifecycle_command(aops_pool, owner, bus=event_bus, resume_turn=False)

    assert kind == "restart"
    assert _browser_release_attempts(loguru_records, agent_id) == []


async def test_nothing_claimed_applies_and_cleans_nothing(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)

    assert await apply_lifecycle_command(aops_pool, owner, bus=event_bus, resume_turn=False) is None
    assert _browser_release_attempts(loguru_records, agent_id) == []


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("unknown event bus defect"), asyncio.CancelledError("publication cancelled")],
    ids=["error", "cancelled"],
)
async def test_a_publication_failure_still_releases_pages_and_cleans_shells_and_is_raised(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: LogRecords,
    failure: BaseException,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_terminate(db_conn, aops_pool, database_gate, kill_shells=True)

    with pytest.raises(type(failure)) as caught:
        await apply_lifecycle_command(
            aops_pool, owner, bus=PublicationDefectBus(failure), resume_turn=False
        )

    assert caught.value is failure
    assert len(_browser_release_attempts(loguru_records, owner.agent_id)) == 1
    assert len(_shell_cleanup_attempts(loguru_records, owner.agent_id)) == 1
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (owner.agent_id,)
    ).fetchone() == ("terminated",)


async def test_a_reaped_corpse_is_announced_and_its_pages_released(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    bus_tap: BusTap,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    db_conn.execute(
        "UPDATE agents_meta SET status='idling', runtime_generation=NULL, runtime_owner=NULL, "
        "last_turn_fatal_at = now() - interval '1 hour' WHERE id=%s",
        (owner.agent_id,),
    )
    db_conn.commit()
    await bus_tap.agent_updated_ids()

    reaped = await reap_and_announce_corpses(aops_pool, "claim-test", bus=event_bus)

    assert [corpse.agent_id for corpse in reaped] == [agent_id]
    assert await bus_tap.agent_updated_ids() == [agent_id]
    assert len(_browser_release_attempts(loguru_records, agent_id)) == 1


async def test_a_publication_defect_does_not_stop_the_next_corpse_from_being_released(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: LogRecords,
) -> None:
    corpses = [agent_row(db_conn), agent_row(db_conn)]
    db_conn.execute(
        "UPDATE agents_meta SET status='idling', last_turn_fatal_at = now() - interval '1 hour' "
        "WHERE id = ANY(%s)",
        (corpses,),
    )
    db_conn.commit()

    reaped = await reap_and_announce_corpses(
        aops_pool, "claim-test", bus=PublicationDefectBus(RuntimeError("unknown bus defect"))
    )

    assert sorted(corpse.agent_id for corpse in reaped) == sorted(corpses)
    assert [
        r["extra"]["agent_id"] for r in loguru_records.events("corpse_reaper_publish_failed")
    ] == [corpse.agent_id for corpse in reaped]
    for corpse in reaped:
        assert len(_browser_release_attempts(loguru_records, corpse.agent_id)) == 1
