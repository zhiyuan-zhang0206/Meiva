from __future__ import annotations

import asyncio
from dataclasses import replace

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.test_harness_turn import FakeEngine, config, deliver, runtime
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.history.harness_messages import aload_harness_timeline_items
from base.events.live.tests.live_capture import LiveEvents
from base.lm.harness import AssistantText, ForegroundAccepted, HarnessTurn
from base.lm.harness.engine import TurnEventSink
from base.native_process.turn_identity import HostedTurnResources


class _Foreground(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.running = asyncio.Event()
        self.control_received = asyncio.Event()
        self.continue_work = asyncio.Event()
        self.terminal = asyncio.Event()
        self.steering_available = False
        self.cancel_received = False

    async def run_turn(
        self, text: str, *, key: str, on_event: TurnEventSink | None = None
    ) -> HarnessTurn:
        assert on_event is not None
        await deliver(on_event, ForegroundAccepted(turn_id="t1"))
        self.running.set()
        await self.continue_work.wait()
        await deliver(on_event, AssistantText(turn_id="t1", text="Last observed activity"))
        await self.terminal.wait()
        self.steering_available = False
        return replace(await super().run_turn(text, key=key), interrupted=self.cancel_received)

    async def interrupt(self) -> object:
        self.cancel_received = True
        self.control_received.set()
        return object()

    async def steer(self, text: str, *, key: str) -> bool:
        self.control_received.set()
        await self.continue_work.wait()
        assert "keep the logs" in text
        assert key.startswith("inbound-")
        return True


async def test_cancel_keeps_reading_until_the_foreground_ends(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = agent_row(db_conn)
    engine = _Foreground()
    state = AgentState(harness_pending_input={"prompt": "work", "key": "initial"})
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
    )
    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        turn = tasks.create_task(harness_turn_node(state, rt, config(agent_id)))
        await engine.running.wait()
        _ = db_conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'cancel', 'user')",
            (agent_id,),
        )
        db_conn.commit()
        await engine.control_received.wait()
        assert not turn.done()
        assert "cancelled" not in await live_events.roles()
        engine.continue_work.set()
        engine.terminal.set()

    events = await live_events.events()
    assert events[-1]["role"] == "cancelled"
    assert any(
        item["payload"] == "Last observed activity"
        for event in events
        for item in event.get("items", [])
    )
    assert not engine.closed


async def test_steer_moves_from_pending_to_timeline_only_after_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = agent_row(db_conn)
    engine = _Foreground()
    engine.steering_available = True
    state = AgentState(harness_pending_input={"prompt": "work", "key": "initial"})
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
    )
    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        _ = tasks.create_task(harness_turn_node(state, rt, config(agent_id)))
        await engine.running.wait()
        row = db_conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, 'keep the logs', 'chat', 'user') RETURNING id",
            (agent_id,),
        ).fetchone()
        db_conn.commit()
        assert row is not None
        inbound_id = row[0]
        await engine.control_received.wait()
        queued = db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (inbound_id,)
        ).fetchone()
        assert queued == ("pending",)
        assert "inbound_committed" not in await live_events.roles()
        engine.continue_work.set()
        engine.terminal.set()

    async with aops_pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, agent_id)
    assert [item.inbound_id for item in items if item.kind == "inbound_chat"] == [inbound_id]
    events = await live_events.events()
    assert [event["inbound_id"] for event in events if event["role"] == "inbound_committed"] == [
        inbound_id
    ]
    assert events[-1]["role"] == "llm_done"
