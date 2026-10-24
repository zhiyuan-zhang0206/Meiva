# meiva:note
# The held path's one owed checkpoint write (#5468): retried inside the wake, and failed
# closed when the budget runs out.

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.ownership.tests.ownership_support import admit_owner, agent_row
from base import telemetry
from base.agents.context import MeivaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.messages.timestamp_policy import TimestampSource
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from base.paths import meiva_home
from services.agent_runner.agent_host.held_settle import settle_owed_handoff
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import (
    configured_machine,
    configured_policy,
)

_MACHINE = "claim-test"


# meiva:note
# A real graph whose first `failures` state reads fail the way a checkpoint fault does, so the
# settle under test fails for real instead of being replaced by a stub.
class FailingReads:
    def __init__(self, inner: Any, failures: int) -> None:
        self._inner = inner
        self.failures = failures

    async def aget_state(self, config: Any) -> Any:
        if self.failures:
            self.failures -= 1
            raise OSError("checkpoint store unavailable")
        return await self._inner.aget_state(config)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture
def event_pipeline(
    database: Database, machine_identity: MachineIdentity, unit_home: Path
) -> Iterator[telemetry.EventPipeline]:
    """Own the process writer used by the host operation and mirror barrier."""
    pipeline = telemetry.build_pipeline(
        database=lambda: database, machine=machine_identity, home=unit_home
    )
    try:
        telemetry.init_telemetry(
            process="test-agent-host", pipeline=pipeline, machine_reader=machine_identity.name
        )
        yield pipeline
    finally:
        pipeline.stop(timeout=2)


# meiva:note
# The state the held path owes a write for: a lease that ended without its receipt.
def _owed_lease(db_conn: psycopg.Connection, agent_id: int) -> None:
    db_conn.execute(
        "INSERT INTO agent_impersonations"
        "(id,agent_id,session_id,source,machine,status,ttl_seconds,expires_at,ended_at) "
        "VALUES (gen_random_uuid(),%s,0,'external_agent:codex:test',%s,'released',300,"
        "clock_timestamp(),clock_timestamp())",
        (agent_id, _MACHINE),
    )
    db_conn.commit()


def _row(db_conn: psycopg.Connection, agent_id: int) -> tuple[Any, ...]:
    row = db_conn.execute(
        "SELECT status, last_turn_fatal_at, termination_source FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row is not None, f"agents_meta row {agent_id} missing"
    return row


def _receipt_applied(db_conn: psycopg.Connection, agent_id: int) -> bool:
    row = db_conn.execute(
        "SELECT handoff_applied_at IS NOT NULL FROM agent_impersonations WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return bool(row[0])


async def _graph(pool: AsyncConnectionPool[Any], agent_id: int) -> tuple[Any, AsyncPostgresSaver]:
    async def idle(_state: states.BaseAgentState) -> dict[str, Any]:
        return {}

    saver = AsyncPostgresSaver(pool)
    await saver.setup()
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(states.AgentState, context_schema=MeivaContext)
    builder.add_node("idle", idle)
    builder.add_edge(START, "idle")
    return cast(CompiledStateGraph[Any, Any, Any, Any], builder.compile(checkpointer=saver)), saver


def _host(
    *,
    pool: AsyncConnectionPool[Any],
    saver: AsyncPostgresSaver,
    graph: Any,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
) -> AgentHost:
    return AgentHost(
        handoff_stamping=TimestampSource(
            clock=Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS),
            timestamps_enabled=False,
        ),
        home=meiva_home(),
        policy=configured_policy(),
        pool=pool,
        checkpointer=saver,
        graph=graph,
        machine=configured_machine(name=_MACHINE),
        bus=event_bus,
        db=database,
        catalog=model_catalog,
        commands=CommandCatalog.system(skills=list),
    )


# meiva:note
# A second attempt inside the wake is what makes the failure survivable: the receipt lands,
# and the crash marker this wake stamped on the first attempt goes away — only the marker it
# stamped itself, which is what the CAS is for.
@pytest.mark.usefixtures("event_pipeline")
async def test_the_held_path_settles_the_handoff_after_one_failed_attempt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    incarnation = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    _owed_lease(db_conn, agent_id)
    graph, saver = await _graph(aops_pool, agent_id)
    host = _host(
        pool=aops_pool,
        saver=saver,
        graph=FailingReads(graph, 1),
        database=database,
        event_bus=event_bus,
        model_catalog=model_catalog,
    )
    try:
        await settle_owed_handoff(host._held_settle, agent_id, incarnation)
    finally:
        await host.aclose()

    assert _receipt_applied(db_conn, agent_id)
    assert host.stats.handoff_settle_retries == 1
    assert host.stats.handoff_settle_failures == 0
    assert _row(db_conn, agent_id)[1] is None
    # meiva:note
    # The wake still owns the row: nothing here idles it behind the caller's back.
    assert _row(db_conn, agent_id)[0] == "running"


# meiva:note
# Exhausting the budget must leave a row the reaper can see: the marker says the turn died,
# the idling status is what the reaper's own predicate requires, and the error still reaches
# the caller so the wake keeps its crash accounting.
@pytest.mark.usefixtures("event_pipeline")
async def test_an_exhausted_settle_budget_fails_closed_with_a_reapable_row(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    incarnation = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    _owed_lease(db_conn, agent_id)
    graph, saver = await _graph(aops_pool, agent_id)
    host = _host(
        pool=aops_pool,
        saver=saver,
        graph=FailingReads(graph, 99),
        database=database,
        event_bus=event_bus,
        model_catalog=model_catalog,
    )
    try:
        with pytest.raises(OSError):
            await settle_owed_handoff(host._held_settle, agent_id, incarnation)
    finally:
        await host.aclose()

    status, marker, termination = _row(db_conn, agent_id)
    assert status == "idling"
    assert marker is not None
    assert termination is None
    assert not _receipt_applied(db_conn, agent_id)
    assert host.stats.handoff_settle_retries == 2
    assert host.stats.handoff_settle_failures == 1
