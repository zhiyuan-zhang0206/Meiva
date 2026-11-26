# meiva:note
# The stop button's passive note remains deliverable after confirmation. It rides in the next
# input as text; Meiva lifecycle hints do not invent rows in the external record timeline.
# It also holds how a harness session killed by the host's own graceful exit settles its turn,
# which is a restart's interrupt, not a crash.
# meiva:link agent/graph/docs/turn-interrupts.md

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_interrupt import announce_interrupt
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    completed,
    in_a_turn,
    input_key,
    insert_row,
    read_recorded_items,
    row_status,
    silent_engine,
    wait_until,
)
from agent.graph.claim.tests.harness_support import harness_runtime as harness_runtime
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.graph.llm_errors import FatalLLMStreamError
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.tests.langgraph_views import command_update
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.lm.harness import HarnessInput, InputInContext, SessionFailed, TurnOrigin, TurnStarted
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.turn_identity import HostedTurnResources

_NOTE = "Your running turn was interrupted by user."
_PASSIVE_NOTE = '{"note_tag": "lifecycle_interrupt", "passive": true}'


async def _markers(pool: AsyncConnectionPool, agent_id: int) -> list[str]:
    async with pool.connection() as conn:
        items = await read_recorded_items(conn, agent_id)
    return [item.payload for item in items if item.source == "lifecycle_interrupt"]


async def _stop_a_running_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    *,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    payload: str = _PASSIVE_NOTE,
) -> tuple[int, int, int]:
    agent_id = agent_row(db_conn)
    work = insert_row(db_conn, agent_id, "chat", "user", "work")
    engine = silent_engine()
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )
    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await wait_until(lambda: bool(engine.delivered), "delivery")
        engine.emit(
            TurnStarted("t1", TurnOrigin.INPUT, (input_key(work),), 1),
            InputInContext(input_key(work), "t1", 1),
        )
        control = insert_row(db_conn, agent_id, "interrupt", "user", "")
        note = insert_row(db_conn, agent_id, "system_note", "user", _NOTE, payload=payload)
        await wait_until(lambda: engine.interrupts == 1, "the foreground interrupts")
        assert await _markers(aops_pool, agent_id) == [], (
            "nothing is written before it is confirmed"
        )
        engine.emit(completed("t1", "", interrupted=True))
        await node
    return agent_id, note, control


async def test_a_confirmed_stop_keeps_its_note_pending_without_inventing_a_record_row(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id, note, _control = await _stop_a_running_turn(
        db_conn, aops_pool, hosted_resources, database_gate=database_gate, event_bus=event_bus
    )

    assert await _markers(aops_pool, agent_id) == []
    assert row_status(db_conn, note) == "pending", "the note still waits for a real input"


async def test_the_next_input_carries_the_note_as_text_and_adds_no_second_entry(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id, note, _control = await _stop_a_running_turn(
        db_conn, aops_pool, hosted_resources, database_gate=database_gate, event_bus=event_bus
    )
    follow_up = insert_row(db_conn, agent_id, "chat", "user", "carry on")
    engine = silent_engine()
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await wait_until(lambda: bool(engine.delivered), "the follow-up is delivered")
        keys = [item.key for batch in engine.delivered for item in batch]
        engine.emit(*in_a_turn(keys, "MEIVA_DISPLAY_TO_USER\nok", turn_id="t2"))
        await node

    texts = "\n".join(item.text for batch in engine.delivered for item in batch)
    assert _NOTE in texts and "carry on" in texts
    assert row_status(db_conn, note) == "done" and row_status(db_conn, follow_up) == "done"
    assert await _markers(aops_pool, agent_id) == []


async def test_a_pending_stop_note_is_delivered_as_text_without_a_lifecycle_record_row(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    note = insert_row(db_conn, agent_id, "system_note", "user", _NOTE, payload=_PASSIVE_NOTE)
    follow_up = insert_row(db_conn, agent_id, "chat", "user", "carry on")
    engine = silent_engine()
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await wait_until(lambda: bool(engine.delivered), "delivery")
        keys = [item.key for batch in engine.delivered for item in batch]
        engine.emit(*in_a_turn(keys, "MEIVA_DISPLAY_TO_USER\nok"))
        await node

    assert await _markers(aops_pool, agent_id) == []
    assert row_status(db_conn, note) == row_status(db_conn, follow_up) == "done"
    assert _NOTE in "\n".join(item.text for batch in engine.delivered for item in batch)


async def test_a_note_that_is_not_passive_is_left_to_its_message(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id, note, _control = await _stop_a_running_turn(
        db_conn,
        aops_pool,
        hosted_resources,
        database_gate=database_gate,
        event_bus=event_bus,
        payload='{"note_tag": "lifecycle_interrupt"}',
    )

    assert await _markers(aops_pool, agent_id) == []
    assert row_status(db_conn, note) == "pending"


async def test_announce_interrupt_emits_cancelled_and_leaves_pending_note_untouched(
    live_events: LiveEvents,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    note = insert_row(db_conn, agent_id, "system_note", "user", _NOTE, payload=_PASSIVE_NOTE)
    rt = runtime(
        silent_engine(),
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    announce_interrupt(rt.context, agent_id)

    assert any("cancelled" in payload.lower() for payload in await live_events.payloads())
    assert row_status(db_conn, note) == "pending"
    row = db_conn.execute("SELECT payload FROM inbound_messages WHERE id=%s", (note,)).fetchone()
    assert row is not None and row[0] == {"note_tag": "lifecycle_interrupt", "passive": True}


class _CliKilledEngine(FakeEngine):
    async def deliver(self, inputs: Sequence[HarnessInput], *, compaction_note: str = "") -> None:
        self.emit(SessionFailed("Command failed with exit code 143"))


async def test_a_session_killed_during_the_hosts_graceful_exit_is_handed_to_the_restart(
    loguru_records: LogRecords, db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)
    insert_row(db_conn, agent_id, "restart", "system:restart", "")
    engine = _CliKilledEngine()
    rt = harness_runtime(engine, agent_id=agent_id)
    exiting = Runtime(context=replace(rt.context, host_exiting=lambda: True))

    command = await harness_turn_node(
        AgentState(harness_inputs=[{"key": "inbound-7", "prompt": "do it"}]),
        exiting,
        config(agent_id),
    )

    assert command.goto == "claim"
    update = command_update(command)
    assert update["halted"] is True
    assert update["interrupted_by_restart"] is True
    assert update["harness_inputs"] == []
    assert "session unavailable" in engine.retired, "the dead session is retired, not reused"
    [cut] = [m for m in loguru_records.messages() if "graceful exit" in m]
    assert "exit code 143" in cut


async def test_a_session_killed_outside_a_graceful_exit_is_still_a_fatal_stream_abort(
    loguru_records: LogRecords, db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)

    with pytest.raises(FatalLLMStreamError, match="exit code 143"):
        await harness_turn_node(
            AgentState(harness_inputs=[{"key": "inbound-7", "prompt": "do it"}]),
            harness_runtime(_CliKilledEngine(), agent_id=agent_id),
            config(agent_id),
        )
    assert not [m for m in loguru_records.messages() if "graceful exit" in m]


async def test_a_session_killed_before_the_exit_wrote_its_restart_is_still_a_fatal_stream_abort(
    loguru_records: LogRecords, db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)
    rt = harness_runtime(_CliKilledEngine(), agent_id=agent_id)
    exiting = Runtime(context=replace(rt.context, host_exiting=lambda: True))

    with pytest.raises(FatalLLMStreamError, match="exit code 143"):
        await harness_turn_node(
            AgentState(harness_inputs=[{"key": "inbound-7", "prompt": "do it"}]),
            exiting,
            config(agent_id),
        )
    assert not [m for m in loguru_records.messages() if "graceful exit" in m]
