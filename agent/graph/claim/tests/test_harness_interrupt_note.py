# meiva:note
# The stop button's passive note on an external harness: it is on the timeline as soon as the interrupt is
# confirmed, rides in the harness's next input as text, and is never written to the timeline twice.
# meiva:link agent/graph/docs/turn-interrupts.md

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_inbox import write_interrupt_notes
from agent.graph.claim.harness_interrupt import announce_interrupt
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    completed,
    in_a_turn,
    input_key,
    insert_row,
    row_status,
    silent_engine,
    wait_until,
)
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.history.harness_messages import aload_harness_timeline_items
from base.lm.harness import InputInContext, TurnOrigin, TurnStarted
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.turn_identity import HostedTurnResources

_NOTE = "Your running turn was interrupted by user."
_PASSIVE_NOTE = '{"note_tag": "lifecycle_interrupt", "passive": true}'


async def _markers(pool: AsyncConnectionPool, agent_id: int) -> list[str]:
    async with pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, agent_id)
    return [item.payload for item in items if item.source == "lifecycle_interrupt"]


async def _stop_a_running_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    *,
    payload: str = _PASSIVE_NOTE,
) -> tuple[int, int, int]:
    agent_id = agent_row(db_conn)
    work = insert_row(db_conn, agent_id, "chat", "user", "work")
    engine = silent_engine()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)
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


async def test_a_confirmed_stop_puts_its_note_on_the_timeline_without_delivering_it(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id, note, _control = await _stop_a_running_turn(db_conn, aops_pool, hosted_resources)

    [marker] = await _markers(aops_pool, agent_id)
    assert marker.endswith(_NOTE)
    assert row_status(db_conn, note) == "pending", "the note still waits for a real input"


async def test_the_next_input_carries_the_note_as_text_and_adds_no_second_entry(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id, note, _control = await _stop_a_running_turn(db_conn, aops_pool, hosted_resources)
    follow_up = insert_row(db_conn, agent_id, "chat", "user", "carry on")
    engine = silent_engine()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await wait_until(lambda: bool(engine.delivered), "the follow-up is delivered")
        keys = [item.key for batch in engine.delivered for item in batch]
        engine.emit(*in_a_turn(keys, "MEIVA_DISPLAY_TO_USER\nok", turn_id="t2"))
        await node

    texts = "\n".join(item.text for batch in engine.delivered for item in batch)
    assert _NOTE in texts and "carry on" in texts
    assert row_status(db_conn, note) == "done" and row_status(db_conn, follow_up) == "done"
    assert len(await _markers(aops_pool, agent_id)) == 1


async def test_a_stop_whose_note_was_never_written_is_delivered_with_its_entry(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    insert_row(db_conn, agent_id, "system_note", "user", _NOTE, payload=_PASSIVE_NOTE)
    insert_row(db_conn, agent_id, "chat", "user", "carry on")
    engine = silent_engine()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await wait_until(lambda: bool(engine.delivered), "delivery")
        keys = [item.key for batch in engine.delivered for item in batch]
        engine.emit(*in_a_turn(keys, "MEIVA_DISPLAY_TO_USER\nok"))
        await node

    assert len(await _markers(aops_pool, agent_id)) == 1


async def test_a_note_that_is_not_passive_is_left_to_its_message(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id, note, _control = await _stop_a_running_turn(
        db_conn, aops_pool, hosted_resources, payload='{"note_tag": "lifecycle_interrupt"}'
    )

    assert await _markers(aops_pool, agent_id) == []
    assert row_status(db_conn, note) == "pending"


# meiva:note
# A note an input already reserved is that input's to write: it settles with its own timeline entry, so
# the foreground's write leaves the claimed row alone and unflagged.
async def test_a_note_already_reserved_by_an_input_is_not_flagged(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    note = insert_row(db_conn, agent_id, "system_note", "user", _NOTE, payload=_PASSIVE_NOTE)
    db_conn.execute(
        "UPDATE inbound_messages SET status='claimed', claimed_at=now() WHERE id=%s", (note,)
    )
    db_conn.commit()
    rt = runtime(silent_engine(), ops_pool=aops_pool, hosted_resources=hosted_resources)

    written = await write_interrupt_notes(rt.context, agent_id)

    assert written == []
    row = db_conn.execute("SELECT payload FROM inbound_messages WHERE id=%s", (note,)).fetchone()
    assert row is not None and "timeline_written" not in row[0]
    assert await _markers(aops_pool, agent_id) == []


# meiva:note
# The note is display: a store that cannot take it is reported with its event, the live view is still
# told the turn ended, and the row stays pending and unflagged so its next delivery carries the entry.
async def test_a_note_that_cannot_be_written_is_reported_and_the_row_stays_deliverable(
    db_conn: psycopg.Connection,
    db_url: str,
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
) -> None:
    agent_id = agent_row(db_conn)
    note = insert_row(db_conn, agent_id, "system_note", "user", _NOTE, payload=_PASSIVE_NOTE)
    closed: AsyncConnectionPool = AsyncConnectionPool(db_url, open=False)
    publisher = MagicMock()
    rt = runtime(
        silent_engine(), ops_pool=closed, hosted_resources=hosted_resources, publisher=publisher
    )

    await announce_interrupt(rt.context, agent_id)

    assert len(loguru_records.events("harness_interrupt_note_failed")) == 1
    assert "cancelled" in str(publisher.emit.call_args).lower()
    assert row_status(db_conn, note) == "pending"
    row = db_conn.execute("SELECT payload FROM inbound_messages WHERE id=%s", (note,)).fetchone()
    assert row is not None and "timeline_written" not in row[0]
