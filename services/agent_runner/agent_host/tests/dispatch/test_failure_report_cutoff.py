import asyncio
import random
from collections.abc import Generator
from contextlib import contextmanager
from functools import partial
from typing import LiteralString
from uuid import uuid4

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from redis.asyncio.client import PubSub

from agent.graph import build_graph
from agent.graph.exec.parent_facts import read_parent_facts
from agent.graph.llm_errors import FatalProviderError
from agent.ownership.hosted import admit_hosted_runtime
from agent.ownership.tests.ownership_support import PublicationDefectBus
from agent.state import BaseAgentState
from agent.turn.runloop import PendingTurnFailure, publish_turn_failure
from base.agents.context import MeivaContext
from base.agents.recovery.breaker import admit_pending_wake
from base.agents.tests.units import spawn_agent
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus, EventBusConfig
from base.events.live.projection import Error
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.packages.plugins.extensions import ExtensionRegistry
from services.agent_runner.agent_host import settlement
from services.agent_runner.agent_host.invocation import finish_pending_failure
from services.agent_runner.agent_host.runtime import TurnOutcome
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.host_support import EventTap
from services.agent_runner.agent_host.tests.runtime.host_support import event_tap as event_tap


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


def _failure() -> PendingTurnFailure:
    return PendingTurnFailure(
        FatalProviderError("expected refusal", error_class="permanent", provider="test", status=402)
    )


def _insert(conn: psycopg.Connection, agent_id: int, source: str, *, claimed: bool = False) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,status) "
        "VALUES(%s,'new explicit input','chat',%s,%s) RETURNING id",
        (agent_id, source, "claimed" if claimed else "pending"),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


async def _close(
    pool: AsyncConnectionPool,
    bus: EventBus,
    incarnation: RuntimeIncarnation,
    outcome: TurnOutcome,
) -> None:
    await settlement.close_hosted_turn(
        pool,
        pool,
        bus,
        incarnation,
        outcome,
    )


async def _admit(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    database: Database,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentityReader,
) -> RuntimeIncarnation:
    agent_id = spawn_agent(
        catalog=catalog, authority=authority, database_gate=database_gate, machine=local_machine
    )
    machine = conn.execute("SELECT machine FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
    assert machine is not None
    incarnation = await admit_hosted_runtime(pool, agent_id, machine[0], uuid4(), db=database)
    assert incarnation is not None
    return incarnation


async def test_error_visible_after_cutoff_allows_immediate_new_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    database: Database,
    db_url: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    incarnation = await _admit(
        db_conn,
        aops_pool,
        database,
        model_catalog,
        config_authority,
        database_gate,
        machine_identity,
    )
    agent_id = incarnation.agent_id
    old_claim = _insert(db_conn, agent_id, "user", claimed=True)
    pending = _failure()
    publisher = AgentEventPublisher(
        event_bus.async_redis(),
        event_bus.channel,
        agent_id=agent_id,
        auth_retry=event_bus.auth_retry,
    )
    ctx = MeivaContext(
        ops_pool=aops_pool,
        event_publisher=publisher,
        bus=event_bus,
        db=database,
        catalog=model_catalog,
        agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
        clock_factory=configured_policy().clock_factory,
        original_incarnation=incarnation,
    )
    received: list[Error] = []
    new_chats: list[int] = []
    async with AsyncPostgresSaver.from_conn_string(db_url) as saver:
        graph = build_graph(saver, ExtensionRegistry(), parent=read_parent_facts(), rng=_rng())
        config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
        async with asyncio.TaskGroup() as workers:
            await publisher.start(workers)
            initial_turn = graph.with_types(input_type=BaseAgentState, output_type=BaseAgentState)
            await initial_turn.ainvoke(
                BaseAgentState(halted=True), config, context=ctx, interrupt_before=["init_context"]
            )
            outcome = await finish_pending_failure(graph, saver, agent_id, ctx, config, pending)
            assert len(pending.errors) == 2
            outcome = await finish_pending_failure(graph, saver, agent_id, ctx, config, pending)
            assert len(pending.errors) == 2
            assert not await admit_pending_wake(aops_pool, agent_id)
            await publisher.aclose()
        async with (
            event_bus.open_async_redis() as client,
            PubSub(client.connection_pool) as subscription,
        ):
            await subscription.subscribe(event_bus.channel)

            async def react_to_error() -> None:
                async with asyncio.timeout(5):
                    while len(received) < 2:
                        message = await subscription.get_message(
                            ignore_subscribe_messages=True, timeout=1
                        )
                        if message is None:
                            continue
                        payload = message["data"]
                        if not isinstance(payload, str) or '"role":"error"' not in payload:
                            continue
                        received.append(Error.model_validate_json(payload))
                        if len(received) == 1:
                            row = db_conn.execute(
                                "SELECT status,last_turn_fatal_at FROM agents_meta WHERE id=%s",
                                (agent_id,),
                            ).fetchone()
                            assert row is not None and row[0] == "idling" and row[1] is not None
                            await asyncio.to_thread(_insert, db_conn, agent_id, "system:heartbeat")
                            assert not await admit_pending_wake(aops_pool, agent_id)
                            new_chats.append(
                                await asyncio.to_thread(_insert, db_conn, agent_id, "user")
                            )
                            assert await admit_pending_wake(aops_pool, agent_id)

            async with asyncio.TaskGroup() as reactions:
                reactions.create_task(react_to_error())
                await _close(aops_pool, event_bus, incarnation, outcome)
            await _close(aops_pool, event_bus, incarnation, outcome)
    assert len(received) == 2 and all(error.blocked and error.status == 402 for error in received)
    assert pending.errors == [] and outcome.failure_report is None
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (old_claim,)
    ).fetchone() == ("done",)
    row = db_conn.execute(
        "SELECT i.status,i.created_at>m.last_turn_fatal_at FROM inbound_messages i "
        "JOIN agents_meta m ON m.id=i.agent_id WHERE i.id=%s",
        (new_chats[0],),
    ).fetchone()
    assert row == ("pending", True)
    assert isinstance(pending.error, FatalProviderError)


# meiva:note
# A fault in the settle boundary that a real database can produce: a trigger on the agent row.
# `stamp_refused` makes the corpse stamp raise (the settle itself still lands), `settle_skipped`
# silently drops the write that would idle the row (the stamp lands), and `ownership_replaced`
# gives the row to another incarnation (neither lands).
_FAULTS: dict[str, LiteralString] = {
    "stamp_refused": (
        "CREATE FUNCTION fault_agent_row() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'stamp refused'; END $$; "
        "CREATE TRIGGER fault_agent_row BEFORE UPDATE OF last_turn_fatal_at ON agents_meta "
        "FOR EACH ROW EXECUTE FUNCTION fault_agent_row()"
    ),
    "settle_skipped": (
        "CREATE FUNCTION fault_agent_row() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.status = 'idling' AND OLD.status = 'running' THEN RETURN NULL; END IF; "
        "RETURN NEW; END $$; "
        "CREATE TRIGGER fault_agent_row BEFORE UPDATE OF status ON agents_meta "
        "FOR EACH ROW EXECUTE FUNCTION fault_agent_row()"
    ),
}


# meiva:note
# The fault stays installed for the test and is dropped after it: the throwaway database is
# cleaned of rows between tests, not of functions and triggers.
@contextmanager
def _agent_row_fault(conn: psycopg.Connection, fault: str) -> Generator[None]:
    if fault == "ownership_replaced":
        yield
        return
    conn.execute(_FAULTS[fault])
    conn.commit()
    try:
        yield
    finally:
        conn.execute("DROP TRIGGER fault_agent_row ON agents_meta; DROP FUNCTION fault_agent_row()")
        conn.commit()


@pytest.mark.parametrize("fault", ["stamp_refused", "settle_skipped", "ownership_replaced"])
async def test_unproven_settlement_never_publishes_retry_instructions(
    fault: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    event_tap: EventTap,
    database: Database,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    loguru_records: list[dict[str, object]],
) -> None:
    incarnation = await _admit(
        db_conn,
        aops_pool,
        database,
        model_catalog,
        config_authority,
        database_gate,
        machine_identity,
    )
    if fault == "ownership_replaced":
        db_conn.execute(
            "UPDATE agents_meta SET runtime_generation=%s WHERE id=%s",
            (uuid4(), incarnation.agent_id),
        )
        db_conn.commit()
    pending = _failure()
    pending.errors.append(
        Error(agent_id=incarnation.agent_id, content="original provider refusal", blocked=True)
    )
    outcome = TurnOutcome(
        exited=False,
        crashed=True,
        failure_report=partial(publish_turn_failure, pending, event_bus),
    )

    with _agent_row_fault(db_conn, fault):
        await _close(aops_pool, event_bus, incarnation, outcome)

    assert await event_tap.events("error") == []
    assert outcome.failure_report is not None
    assert len(pending.errors) == 1
    assert any("turn Error withheld" in str(row["message"]) for row in loguru_records)


@pytest.mark.parametrize("fault", ["unknown", "unavailable", "cancel"])
async def test_publication_failure_is_observable_and_never_repeated(
    fault: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    database: Database,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    loguru_records: list[dict[str, object]],
) -> None:
    incarnation = await _admit(
        db_conn,
        aops_pool,
        database,
        model_catalog,
        config_authority,
        database_gate,
        machine_identity,
    )
    agent_id = incarnation.agent_id
    original = ValueError("publication failed") if fault == "unknown" else asyncio.CancelledError()
    report_bus: EventBus = (
        EventBus(
            EventBusConfig(redis_url="redis://127.0.0.1:1/0", events_channel="test"),
            rng=seeded_rng(),
        )
        if fault == "unavailable"
        else PublicationDefectBus(original)
    )
    pending = _failure()
    pending.errors.append(
        Error(agent_id=agent_id, content="original provider refusal", blocked=True)
    )
    outcome = TurnOutcome(
        exited=False,
        crashed=True,
        failure_report=partial(publish_turn_failure, pending, report_bus),
    )
    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError) as caught:
            await _close(aops_pool, event_bus, incarnation, outcome)
        assert caught.value is original
    else:
        await _close(aops_pool, event_bus, incarnation, outcome)
    # meiva:note
    # The failed publication is never attempted again: the report is spent, the errors drained.
    await _close(aops_pool, event_bus, incarnation, outcome)
    if fault != "cancel":
        assert any(f"Error for agent {agent_id}" in str(row["message"]) for row in loguru_records)
    assert outcome.failure_report is None and pending.errors == []
    assert isinstance(pending.error, FatalProviderError)
