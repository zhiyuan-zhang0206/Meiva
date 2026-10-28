import psycopg
from psycopg_pool import AsyncConnectionPool

from base.agents.birth.spawn import create_agent_row
from base.agents.inbox.insert import insert_inbound_message
from base.agents.messages.inbound import InboundKind
from base.api_contracts.agent_contract import AgentStatus
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops import lifecycle
from ops.cluster.rpc import RpcClient


async def test_permanent_reject_blocks_heartbeat_but_fresh_chat_can_try_again(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    rpc: RpcClient,
) -> None:
    agent = terminated(
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    db_conn.execute(
        "UPDATE agents_meta SET permanent_reject_streak=1,last_permanent_reject_reason='billing' "
        "WHERE id=%s",
        (agent,),
    )
    heartbeat = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) "
        "VALUES(%s,'Heartbeat','heartbeat','system') RETURNING id",
        (agent,),
    ).fetchone()
    assert heartbeat is not None
    db_conn.commit()
    assert (
        await lifecycle.resurrect_if_terminated(
            database,
            event_bus,
            agent,
            local_machine=machine_identity.name(),
            trigger_inbound_id=heartbeat[0],
            trigger_inbound_kind=InboundKind.HEARTBEAT,
            rpc=rpc,
        )
        is AgentStatus.TERMINATED
    )
    chat = insert_inbound_message(
        db_conn,
        agent,
        "The provider is fixed; continue.",
        "user",
        bus=event_bus,
        database=database,
    )
    assert (
        await lifecycle.resurrect_if_terminated(
            database,
            event_bus,
            agent,
            local_machine=machine_identity.name(),
            trigger_inbound_id=chat,
            trigger_inbound_kind=InboundKind.CHAT,
            rpc=rpc,
        )
        is AgentStatus.IDLING
    )
    assert db_conn.execute(
        "SELECT permanent_reject_streak,last_permanent_reject_reason FROM agents_meta WHERE id=%s",
        (agent,),
    ).fetchone() == (0, None)


def terminated(
    conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentityReader,
) -> int:
    agent, *_ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
        spawner="user",
        machine=local_machine.name(),
        catalog=model_catalog,
        authority=config_authority,
    )
    conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
        (agent,),
    )
    conn.commit()
    return agent


async def test_idling_owner_blocks_background_wake_until_new_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    from base.agents.recovery.breaker import admit_pending_wake

    agent = terminated(
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    db_conn.execute(
        "UPDATE agents_meta SET status='idling',last_turn_fatal_at=clock_timestamp(),permanent_reject_streak=1 WHERE id=%s",
        (agent,),
    )
    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'Heartbeat','heartbeat','system')",
        (agent,),
    )
    db_conn.commit()
    assert not await admit_pending_wake(aops_pool, agent)
    assert not await admit_pending_wake(aops_pool, agent)
    insert_inbound_message(
        db_conn, agent, "The provider is fixed.", "user", bus=event_bus, database=database
    )
    assert await admit_pending_wake(aops_pool, agent)
    assert db_conn.execute(
        "SELECT permanent_reject_streak FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == (0,)


# meiva:note
# The 2026-10-27 fixed point (#5665): an operator restart waits as a *pending* inbound, so a
# gate that only admits an already-claimed restart can never open. A human's restart admits the
# turn; the automatic ones stay blocked, because those are what the breaker exists to stop.
async def test_an_operator_restart_admits_a_turn_while_the_breaker_is_tripped(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    from base.agents.recovery.breaker import admit_pending_wake

    agent = terminated(
        db_conn,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    db_conn.execute(
        "UPDATE agents_meta SET status='idling',last_turn_fatal_at=clock_timestamp(),"
        "permanent_reject_streak=1 WHERE id=%s",
        (agent,),
    )
    db_conn.commit()

    insert_inbound_message(
        db_conn, agent, "", "self", kind="restart", bus=event_bus, database=database
    )
    insert_inbound_message(
        db_conn, agent, "", "system:supervisor", kind="restart", bus=event_bus, database=database
    )
    insert_inbound_message(
        db_conn, agent, "", "self", kind="compact_request", bus=event_bus, database=database
    )
    assert not await admit_pending_wake(aops_pool, agent)
    assert not await admit_pending_wake(aops_pool, agent)

    insert_inbound_message(
        db_conn, agent, "", "user", kind="restart", bus=event_bus, database=database
    )
    assert await admit_pending_wake(aops_pool, agent)
    # meiva:note
    # Admission is not absolution: only a turn that makes progress clears the streak.
    assert db_conn.execute(
        "SELECT permanent_reject_streak FROM agents_meta WHERE id=%s", (agent,)
    ).fetchone() == (1,)
