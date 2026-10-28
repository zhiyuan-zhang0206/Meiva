import asyncio
import time
from collections.abc import Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage
from psycopg_pool import AsyncConnectionPool

from agent.state import AgentState
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from base.paths import meiva_home
from base.sessions.tests.poll_until import poll_until_async
from services.agent_runner.agent_host.dispatcher import InboundWakeDispatcher, TurnScheduler
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import (
    configured_machine,
    configured_policy,
    handoff_timestamps,
)
from services.agent_runner.agent_host.tests.lifecycle.recovery_admission import admit as _admit
from services.agent_runner.agent_host.tests.lifecycle.recovery_admission import (
    checkpoint_graph as _graph,
)


async def test_pending_scan_classifies_lifecycle_work_and_impersonation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    insert_inbound: Callable[..., int],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent,))
    lifecycle = insert_inbound(db_conn, agent, "", "system:test", kind="restart")
    db_conn.commit()
    host = AgentHost(
        runtime_owner=uuid4(),
        handoff_stamping=handoff_timestamps(),
        home=meiva_home(),
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=AsyncMock(),
        graph=AsyncMock(),
        bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog.system(skills=list, home=meiva_home()),
    )

    assert await host.pending_inbound_wakes() == [agent]

    ordinary = insert_inbound(db_conn, agent, "Need a reply", "user")
    db_conn.commit()
    assert await host.pending_inbound_wakes() == [agent]

    db_conn.execute(
        "UPDATE inbound_messages SET status='done' WHERE id IN (%s,%s)", (lifecycle, ordinary)
    )
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,session_id,source,machine,status,"
        "ttl_seconds,expires_at) VALUES(%s,%s,1,'cli',%s,'requested',300,"
        "clock_timestamp()+interval '5 minutes')",
        (uuid4(), agent, machine_identity.name()),
    )
    db_conn.commit()
    assert await host.pending_inbound_wakes() == [agent]


@pytest.mark.parametrize("silent_turn", [False, True])
async def test_old_pending_scan_never_cancels_a_running_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    silent_turn: bool,
    insert_inbound: Callable[..., int],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    inbound = insert_inbound(db_conn, agent, "Queued before host recovery", "user")
    db_conn.execute(
        "UPDATE inbound_messages SET created_at=now()-interval '1 day' WHERE id=%s", (inbound,)
    )
    db_conn.execute(
        "UPDATE agents_meta SET last_active_at=now()-interval '1 day' WHERE id=%s", (agent,)
    )
    db_conn.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    invocations: list[int] = []

    async def work(_state: AgentState) -> dict[str, object]:
        invocations.append(agent)
        host.turn_progress.mark(agent)
        entered.set()
        await release.wait()
        return {"turn_idle": True, "halted": True, "messages": [AIMessage(content="Completed")]}

    graph, saver = await _graph(aops_pool, agent, work)
    host = AgentHost(
        runtime_owner=uuid4(),
        handoff_stamping=handoff_timestamps(),
        home=meiva_home(),
        policy=configured_policy(),
        pool=aops_pool,
        graph=graph,
        checkpointer=saver,
        bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog.system(skills=list, home=meiva_home()),
    )
    host._owner = incarnation.owner
    scheduler = TurnScheduler(host.run_turn, timers=SYSTEM_TIMERS)
    dispatcher = InboundWakeDispatcher(
        EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
        scheduler,
        pending_scan=host.pending_inbound_wakes,
        timers=SYSTEM_TIMERS,
    )
    try:
        scheduler.wake(agent)
        await asyncio.wait_for(entered.wait(), 3)
        assert await host.pending_inbound_wakes() == [agent]
        if silent_turn:
            host.turn_progress._marks[agent] = [time.monotonic() - 86400]
        await dispatcher.scan_once()
        assert agent in scheduler.active_agents
        assert invocations == [agent]
        assert not scheduler.restart_required
    finally:
        release.set()
        await poll_until_async(lambda: not scheduler.active_agents, timeout=3)
        await scheduler.aclose()
        await host.aclose()
    state = await graph.aget_state({"configurable": {"thread_id": str(agent)}})
    assert state.values["messages"][-1].content == "Completed"


@pytest.mark.parametrize("status", ["running", "idling"])
@pytest.mark.parametrize("boundary", ["same_owner", "foreign", "unowned", "fatal", "terminated"])
async def test_owner_recovery_scan_excludes_unrelated_rows(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    status: str,
    boundary: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    incarnation = await _admit(
        aops_pool,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    agent = incarnation.agent_id
    host = AgentHost(
        runtime_owner=uuid4(),
        handoff_stamping=handoff_timestamps(),
        home=meiva_home(),
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=AsyncMock(),
        graph=AsyncMock(),
        bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog.system(skills=list, home=meiva_home()),
    )
    if boundary == "same_owner":
        host._owner = incarnation.owner
    db_conn.execute(
        "UPDATE agents_meta SET status=%s,machine=%s WHERE id=%s",
        (
            "terminated" if boundary == "terminated" else status,
            "another-machine" if boundary == "foreign" else machine_identity.name(),
            agent,
        ),
    )
    if boundary == "unowned":
        db_conn.execute(
            "UPDATE agents_meta SET runtime_owner=NULL,runtime_generation=NULL WHERE id=%s",
            (agent,),
        )
    if boundary == "fatal":
        db_conn.execute("UPDATE agents_meta SET last_turn_fatal_at=now() WHERE id=%s", (agent,))
    db_conn.commit()
    assert await host.pending_inbound_wakes() == []
