# meiva:note
# The held path's one owed checkpoint write (#5468): retried inside the wake, and failed
# closed when the budget runs out.

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.impersonation import handoff_landed
from agent.ownership.corpse_reap import reap_crash_corpses
from agent.ownership.hosted import stamp_turn_fatal
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
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
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
# meiva:note
# A real graph whose state reads take longer than the budget, so the wake times out.
class SleepingGraph:
    def __init__(self, inner: Any, seconds: float) -> None:
        self._inner = inner
        self.seconds = seconds

    async def aget_state(self, config: Any) -> Any:
        await asyncio.sleep(self.seconds)
        return await self._inner.aget_state(config)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


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
    # meiva:note
    # The wake must be wired to the real read: a test that builds `HeldSettle` itself can pass a
    # double, and this assertion keeps that from quietly replacing production behaviour here.
    assert host._held_settle.receipt_landed is handoff_landed
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


async def _landed(_pool: AsyncConnectionPool[Any], _agent_id: int) -> bool:
    return True


# meiva:note
# The timeout branch, both ways. A settle that cannot meet its budget ends in fail-closed with
# the receipt still owed. When the receipt has already landed, the write commits in a thread
# and the await after it is where a cancel lands, the wake must return instead: marking a
# healthy row would spend a heartbeat turn it does not need. The double stands in for that
# thread; the write itself is the retry test above.
async def test_a_timeout_with_the_receipt_unwritten_fails_closed(
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
        graph=SleepingGraph(graph, 5.0),
        database=database,
        event_bus=event_bus,
        model_catalog=model_catalog,
    )
    try:
        with pytest.raises(TimeoutError):
            await settle_owed_handoff(
                replace(host._held_settle, budget_s=0.2), agent_id, incarnation
            )
    finally:
        await host.aclose()

    status, marker, termination = _row(db_conn, agent_id)
    assert status == "idling"
    assert marker is not None
    assert termination is None
    assert not _receipt_applied(db_conn, agent_id)
    assert host.stats.handoff_settle_failures == 1


async def test_a_timeout_after_the_receipt_landed_returns_without_marking(
    loguru_records: LogRecords,
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
        graph=SleepingGraph(graph, 5.0),
        database=database,
        event_bus=event_bus,
        model_catalog=model_catalog,
    )
    held = replace(
        host._held_settle,
        budget_s=0.2,
        receipt_landed=_landed,
    )
    try:
        await settle_owed_handoff(held, agent_id, incarnation)
    finally:
        await host.aclose()

    status, marker, _ = _row(db_conn, agent_id)
    assert status == "running"
    assert marker is None
    assert host.stats.handoff_settle_failures == 0
    assert len(loguru_records.events("hosted_handoff_settle_raced")) == 1
    assert loguru_records.events("hosted_handoff_settle_failed") == []


# meiva:note
# The sequence the reaper would have got wrong: wake A stamps T1 and fails closed, wake B admits
# later (T2 > T1) and settles the receipt. A timestamp test keeps T1 (COALESCE preserves the
# first death) and leaves the marker on a row that is now healthy, so fifteen minutes later the
# reaper terminates an agent that owes nothing. Asserting the reap attempt as well keeps that
# consequence visible, not just the row's columns.
async def test_a_marker_from_an_earlier_wake_is_cleared_by_the_wake_that_settles(
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
    assert (await stamp_turn_fatal(aops_pool, incarnation)).applied
    db_conn.execute(
        "UPDATE agents_meta SET last_turn_fatal_at = now() - interval '10 minutes' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()
    assert _row(db_conn, agent_id)[1] is not None

    graph, saver = await _graph(aops_pool, agent_id)
    host = _host(
        pool=aops_pool,
        saver=saver,
        graph=graph,
        database=database,
        event_bus=event_bus,
        model_catalog=model_catalog,
    )
    try:
        await settle_owed_handoff(host._held_settle, agent_id, incarnation)
    finally:
        await host.aclose()

    assert _receipt_applied(db_conn, agent_id)
    assert _row(db_conn, agent_id)[1] is None
    db_conn.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (agent_id,))
    db_conn.commit()
    assert await reap_crash_corpses(aops_pool, _MACHINE) == []
    # meiva:note
    # The contrast that makes the assertion above load-bearing: put the stale marker back, past
    # the grace, and the reaper takes the row — which is exactly what a timestamp-based clear
    # would have left behind, one terminated healthy agent at a time.
    db_conn.execute(
        "UPDATE agents_meta SET last_turn_fatal_at = now() - interval '16 minutes' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()
    assert [corpse.agent_id for corpse in await reap_crash_corpses(aops_pool, _MACHINE)] == [
        agent_id
    ]
