# meiva:note
# Delivery of the inbox to an external harness: one path, settled by the harness's own facts.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import FakeEngine, completed, in_a_turn, rejecting
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.ownership.tests.ownership_support import admit_owner, agent_row
from agent.state import AgentState
from base.agents.history.harness_messages import aload_harness_timeline_items
from base.agents.inbox.rows import list_pending_inbounds
from base.db.code_version_gate import ProcessDbGate
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    HarnessInput,
    InputInContext,
    InputLost,
    TurnOrigin,
    TurnStarted,
)
from base.native_process.turn_identity import HostedTurnResources


def _insert(
    db: psycopg.Connection,
    agent_id: int,
    kind: str,
    source: str,
    content: str,
    payload: str | None = None,
) -> int:
    row = db.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
        "VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
        (agent_id, content, kind, source, payload),
    ).fetchone()
    db.commit()
    assert row is not None
    return row[0]


def _status(db: psycopg.Connection, inbound_id: int) -> object:
    db.commit()
    row = db.execute("SELECT status FROM inbound_messages WHERE id=%s", (inbound_id,)).fetchone()
    assert row is not None
    return row[0]


async def _until(condition: Callable[[], bool], what: str) -> None:
    try:
        async with asyncio.timeout(10):
            while not condition():
                await asyncio.sleep(0.02)
    except TimeoutError as error:
        raise AssertionError(what) from error


def _silent() -> FakeEngine:
    return FakeEngine(script=lambda _inputs: [])


def _key(inbound_id: int) -> str:
    return f"inbound-{inbound_id}"


async def _timeline_inbound_ids(pool: AsyncConnectionPool, agent_id: int) -> list[int | None]:
    async with pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, agent_id)
    return [item.inbound_id for item in items if item.kind == "inbound_chat"]


async def test_an_idle_harness_gets_the_head_of_the_inbox_and_a_turn_begins(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    first = _insert(db_conn, agent_id, "chat", "user", "first")
    second = _insert(
        db_conn, agent_id, "system_note", "system", "a note", payload='{"note_tag": "agent_reply"}'
    )
    engine = FakeEngine()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    command = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert command.goto == "claim"
    [batch] = engine.delivered
    assert [item.key for item in batch] == [_key(first), _key(second)], "one batch, in queue order"
    assert [_status(db_conn, i) for i in (first, second)] == ["done", "done"]
    assert await _timeline_inbound_ids(aops_pool, agent_id) == [first]


async def test_a_message_is_pending_until_it_is_in_the_context_and_then_in_the_timeline(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    db_conn.execute("UPDATE agents_meta SET harness='claude_code' WHERE id=%s", (agent_id,))
    db_conn.commit()
    row = _insert(db_conn, agent_id, "chat", "user", "hello")
    engine = _silent()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await _until(lambda: bool(engine.delivered), "delivery")
        assert _status(db_conn, row) == "claimed"
        assert [r.id for r in list_pending_inbounds(db_conn, agent_id)] == [row], "still queued"
        assert await _timeline_inbound_ids(aops_pool, agent_id) == []
        engine.emit(*in_a_turn([_key(row)], "MEIVA_DISPLAY_TO_USER\nhi"))
        await node

    assert _status(db_conn, row) == "done"
    assert list_pending_inbounds(db_conn, agent_id) == []
    assert await _timeline_inbound_ids(aops_pool, agent_id) == [row]


async def test_a_message_that_arrives_mid_turn_is_delivered_on_the_wake_and_absorbed(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    first = _insert(db_conn, agent_id, "chat", "user", "start")
    engine = _silent()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await _until(lambda: len(engine.delivered) == 1, "first delivery")
        engine.emit(
            TurnStarted("t1", TurnOrigin.INPUT, (_key(first),), 1),
            InputInContext(_key(first), "t1", 1),
        )
        second = _insert(db_conn, agent_id, "chat", "user", "more")
        engine.notify_inbound()
        await _until(lambda: len(engine.delivered) == 2, "the wake delivers without polling")
        assert [i.key for i in engine.delivered[1]] == [_key(second)]
        engine.emit(
            InputInContext(_key(second), "t1", 1), completed("t1", "MEIVA_DISPLAY_TO_USER\ndone")
        )
        await node

    assert [_status(db_conn, i) for i in (first, second)] == ["done", "done"]
    assert await _timeline_inbound_ids(aops_pool, agent_id) == [first, second]


async def test_an_input_deferred_to_a_new_turn_settles_at_ack_two_and_the_call_stays(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    first = _insert(db_conn, agent_id, "chat", "user", "start")
    engine = _silent()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await _until(lambda: len(engine.delivered) == 1, "first delivery")
        engine.emit(
            TurnStarted("t1", TurnOrigin.INPUT, (_key(first),), 1),
            InputInContext(_key(first), "t1", 1),
        )
        second = _insert(db_conn, agent_id, "chat", "user", "too late for the last boundary")
        engine.notify_inbound()
        await _until(lambda: len(engine.delivered) == 2, "second delivery")
        engine.emit(AssistantText("t1", "MEIVA_DISPLAY_TO_USER\nfirst answer"), completed("t1", ""))
        await asyncio.sleep(0.5)
        assert not node.done(), "the turn ended but the input is not settled"
        assert _status(db_conn, second) == "claimed"
        engine.emit(
            TurnStarted("t2", TurnOrigin.HARNESS, (_key(second),), 1),
            InputInContext(_key(second), "t2", 1),
            AssistantText("t2", "MEIVA_DISPLAY_TO_USER\nsecond answer"),
            completed("t2", ""),
        )
        await node

    assert _status(db_conn, second) == "done"
    assert await _timeline_inbound_ids(aops_pool, agent_id) == [first, second]


async def test_a_refused_batch_goes_back_in_order_and_is_delivered_again_after_the_backoff(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    rows = [_insert(db_conn, agent_id, "chat", "user", text) for text in ("a", "b", "c")]
    attempts = 0

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return rejecting()(inputs)
        return in_a_turn([i.key for i in inputs], "MEIVA_DISPLAY_TO_USER\nok")

    engine = FakeEngine(script=script)
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    keys = [[item.key for item in batch] for batch in engine.delivered]
    assert keys == [[_key(i) for i in rows]] * 2, "same keys, same order, delivered twice"
    assert [_status(db_conn, i) for i in rows] == ["done"] * 3


async def test_a_batch_settles_one_input_at_a_time(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    first = _insert(db_conn, agent_id, "chat", "user", "kept")
    second = _insert(db_conn, agent_id, "chat", "user", "lost")
    attempts = 0

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        nonlocal attempts
        attempts += 1
        keys = [i.key for i in inputs]
        if attempts == 1:
            return [
                TurnStarted("t1", TurnOrigin.INPUT, (keys[0],), 1),
                InputInContext(keys[0], "t1", 1),
                InputLost(keys[1], 1),
                completed("t1", "MEIVA_DISPLAY_TO_USER\nfirst only"),
            ]
        return in_a_turn(keys, "MEIVA_DISPLAY_TO_USER\nsecond", turn_id="t2")

    engine = FakeEngine(script=script)
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert [[i.key for i in batch] for batch in engine.delivered] == [
        [_key(first), _key(second)],
        [_key(second)],
    ], "only the input that missed the context is handed over again"
    assert await _timeline_inbound_ids(aops_pool, agent_id) == [first, second]


async def test_a_control_row_is_a_barrier_that_delivery_does_not_cross(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    before = _insert(db_conn, agent_id, "chat", "user", "before")
    barrier = _insert(db_conn, agent_id, "resurrect", "system", "")
    behind = _insert(db_conn, agent_id, "chat", "user", "behind")
    engine = FakeEngine()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    await harness_turn_node(AgentState(), rt, config(agent_id))

    assert [i.key for batch in engine.delivered for i in batch] == [_key(before)]
    assert [_status(db_conn, i) for i in (before, barrier, behind)] == [
        "done",
        "pending",
        "pending",
    ]


async def test_synthetic_inputs_have_no_row_and_reach_the_timeline_at_ack_two(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    marker = {
        "item_id": "0.0",
        "kind": "system_marker",
        "payload": "restart accepted",
        "created_at": "2026-11-11T00:00:00+00:00",
    }
    state = AgentState(
        harness_inputs=[{"key": "control-9", "prompt": "continue", "timeline_items": [marker]}]
    )
    engine = _silent()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(state, rt, config(agent_id)))
        await _until(lambda: bool(engine.delivered), "delivery")
        assert [(i.key, i.text) for i in engine.delivered[0]] == [("control-9", "continue")]
        async with aops_pool.connection() as conn:
            assert await aload_harness_timeline_items(conn, agent_id) == []
        engine.emit(*in_a_turn(["control-9"], "MEIVA_DISPLAY_TO_USER\nok"))
        await node

    async with aops_pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, agent_id)
    assert [item.payload for item in items if item.kind == "system_marker"] == ["restart accepted"]


async def test_a_cancel_interrupts_and_the_call_ends_only_when_the_harness_does(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = _insert(db_conn, agent_id, "chat", "user", "work")
    engine = _silent()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await _until(lambda: bool(engine.delivered), "delivery")
        engine.emit(
            TurnStarted("t1", TurnOrigin.INPUT, (_key(row),), 1),
            InputInContext(_key(row), "t1", 1),
        )
        cancel = _insert(db_conn, agent_id, "cancel", "user", "")
        await _until(lambda: engine.interrupts == 1, "the foreground cancel interrupts")
        await asyncio.sleep(0.2)
        assert not node.done(), "only the harness's terminal event ends the turn"
        engine.emit(completed("t1", "", interrupted=True))
        command = await node

    assert command.goto == "claim"
    assert _status(db_conn, cancel) == "pending", "claim dispatches the cancel next"
    assert not engine.closed


async def test_a_message_not_in_context_when_the_turn_is_interrupted_is_still_delivered(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    first = _insert(db_conn, agent_id, "chat", "user", "work")
    engine = _silent()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await _until(lambda: len(engine.delivered) == 1, "first delivery")
        engine.emit(
            TurnStarted("t1", TurnOrigin.INPUT, (_key(first),), 1),
            InputInContext(_key(first), "t1", 1),
        )
        queued = _insert(db_conn, agent_id, "chat", "user", "queued before the stop")
        engine.notify_inbound()
        await _until(lambda: len(engine.delivered) == 2, "second delivery")
        _insert(db_conn, agent_id, "cancel", "user", "")
        await _until(lambda: engine.interrupts == 1, "interrupt")
        engine.emit(
            completed("t1", "", interrupted=True),
            TurnStarted("t2", TurnOrigin.HARNESS, (_key(queued),), 1),
            InputInContext(_key(queued), "t2", 1),
            completed("t2", "MEIVA_DISPLAY_TO_USER\nhandled"),
        )
        await node

    assert _status(db_conn, queued) == "done"
    assert await _timeline_inbound_ids(aops_pool, agent_id) == [first, queued]


async def test_the_claim_leaves_deliverable_rows_to_delivery_and_takes_leading_controls(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    control = _insert(db_conn, agent_id, "resurrect", "system", "")
    chat = _insert(db_conn, agent_id, "chat", "user", "after the control")
    later = _insert(db_conn, agent_id, "cancel", "user", "")
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)

    batch = await claim_inbound_batch(
        aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
    )

    assert [item.id for item in batch] == [control], "stops before the first deliverable row"
    assert [_status(db_conn, i) for i in (chat, later)] == ["pending", "pending"]
    again = await claim_inbound_batch(
        aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
    )
    assert again == [], "the chat is next, and it is delivery's"
