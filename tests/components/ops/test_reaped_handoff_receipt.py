# meiva:note
# The load-bearing link of the held-path settle ladder (#5468): a row the reaper had to take
# still owes its handoff receipt, so the resurrected agent's first turn must write it.
# Nothing else in the suite covers resurrect x handoff, and without this the deadlock would
# only move from zero seconds to the reap grace.

import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.ownership.corpse_reap import reap_crash_corpses
from agent.ownership.hosted import settle_hosted_runtime, stamp_turn_fatal
from agent.ownership.tests.ownership_support import admit_owner, agent_row
from base import telemetry
from base.agents.context import MeivaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.messages.timestamp_policy import TimestampSource
from base.agents.observation.relay_supervision import RelaySupervision
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentityReader
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.paths import meiva_home
from ops.agents import wake
from services.agent_runner.agent_host.invocation import (
    HandoffSettlement,
    settle_handoff_before_invocation,
)
from services.agent_runner.agent_host.tests.host_policy import configured_policy


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


_MACHINE = "claim-test"


@pytest.fixture
def event_pipeline(
    database: Database, machine_identity: MachineIdentityReader, unit_home: Path
) -> Iterator[telemetry.EventPipeline]:
    """Own the process writer used by the operations under test."""
    pipeline = telemetry.build_pipeline(
        database=lambda: database, machine=machine_identity, home=unit_home
    )
    try:
        telemetry.init_telemetry(
            process="test-reaped-handoff", pipeline=pipeline, machine_reader=machine_identity.name
        )
        yield pipeline
    finally:
        pipeline.stop(timeout=2)


# meiva:note
# The state the ladder owes a write for: a lease that ended without its receipt.
def _owed_lease(db_conn: psycopg.Connection, agent_id: int) -> None:
    db_conn.execute(
        "INSERT INTO agent_impersonations"
        "(id,agent_id,session_id,source,machine,status,ttl_seconds,expires_at,ended_at) "
        "VALUES (gen_random_uuid(),%s,0,'external_agent:codex:test',%s,'released',300,"
        "clock_timestamp(),clock_timestamp())",
        (agent_id, _MACHINE),
    )
    db_conn.commit()


async def _graph(pool: AsyncConnectionPool[Any]) -> tuple[Any, AsyncPostgresSaver]:
    async def idle(_state: states.BaseAgentState) -> dict[str, Any]:
        return {}

    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(states.AgentState, context_schema=MeivaContext)
    builder.add_node("idle", idle)
    builder.add_edge(START, "idle")
    return builder.compile(checkpointer=saver), saver


@pytest.mark.usefixtures("event_pipeline")
async def test_a_reaped_handoff_receipt_lands_on_the_resurrected_agents_first_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = agent_row(db_conn)
    incarnation = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    _owed_lease(db_conn, agent_id)

    # meiva:note
    # The held path failed closed hours ago: crash-marked, idling, marker past the grace.
    assert await stamp_turn_fatal(aops_pool, incarnation)
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus)
    db_conn.execute(
        "UPDATE agents_meta SET last_turn_fatal_at = now() - interval '16 minutes' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()

    reaped = await reap_crash_corpses(aops_pool, _MACHINE)
    assert [corpse.agent_id for corpse in reaped] == [agent_id]
    assert db_conn.execute(
        "SELECT attributes->>'unapplied_handoff_receipt' FROM audit_events "
        "WHERE agent_id = %s AND attributes->>'reason' = 'corpse_reaper'",
        (agent_id,),
    ).fetchall() == [("true",)]

    wake.resurrect_agent(database, event_bus, agent_id, resurrected_by="user")
    assert db_conn.execute(
        "SELECT status, last_turn_fatal_at FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone() == ("idling", None)

    # meiva:note
    # The revived agent's first turn settles the receipt the dead wake never wrote.
    incarnation = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    graph, _saver = await _graph(aops_pool)
    policy = configured_policy()
    ctx = MeivaContext(
        ops_pool=aops_pool,
        original_incarnation=incarnation,
        hosted_resources=None,
        agent=AgentSlices.resolve(default_reader=policy.default_reader),
        machine=machine_identity,
        db=database,
        bus=event_bus,
        event_publisher=MagicMock(),
        catalog=model_catalog,
        clock_factory=policy.clock_factory,
    )
    settlement = HandoffSettlement(
        db=database,
        bus=event_bus,
        relays=RelaySupervision(),
        timestamps=TimestampSource(
            clock=Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS),
            timestamps_enabled=False,
        ),
        local_machine=_MACHINE,
        home=meiva_home(),
        rng=_rng(),
    )
    await settle_handoff_before_invocation(
        graph, ctx, agent_id, fresh_input=False, settlement=settlement
    )
    assert db_conn.execute(
        "SELECT handoff_applied_at IS NOT NULL FROM agent_impersonations WHERE agent_id = %s",
        (agent_id,),
    ).fetchone() == (True,)


# meiva:note
# The audit flag has to separate the two kinds of corpse: an ordinary crash never owed a
# receipt, and reading that as "handoff stuck" would send an operator after nothing.
@pytest.mark.usefixtures("event_pipeline")
async def test_an_ordinary_corpse_is_reaped_without_the_handoff_flag(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    incarnation = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    assert await stamp_turn_fatal(aops_pool, incarnation)
    assert await settle_hosted_runtime(aops_pool, incarnation, bus=event_bus)
    db_conn.execute(
        "UPDATE agents_meta SET last_turn_fatal_at = now() - interval '16 minutes' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()

    assert [corpse.agent_id for corpse in await reap_crash_corpses(aops_pool, _MACHINE)] == [
        agent_id
    ]
    assert db_conn.execute(
        "SELECT attributes->>'unapplied_handoff_receipt' FROM audit_events "
        "WHERE agent_id = %s AND attributes->>'reason' = 'corpse_reaper'",
        (agent_id,),
    ).fetchall() == [("false",)]
