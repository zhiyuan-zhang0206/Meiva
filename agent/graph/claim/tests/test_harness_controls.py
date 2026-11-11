from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.test_harness_turn import FakeEngine, config, deliver, runtime
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.history.harness_messages import aload_harness_timeline_items
from base.db import PG_KEEPALIVE_KWARGS
from base.events.live.tests.live_capture import LiveEvents
from base.host.boundary.json_values import is_object_dict
from base.lm.harness import AssistantText, ForegroundAccepted, HarnessTurn, InputLostError
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
    assert not any(
        item["payload"] == "status:Cancellation requested."
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


class _BatchSteerEngine(_Foreground):
    def __init__(self, expected: int) -> None:
        super().__init__()
        self.steered: list[tuple[str, str]] = []
        self._expected = expected
        self.all_steered = asyncio.Event()

    async def steer(self, text: str, *, key: str) -> bool:
        self.steered.append((text, key))
        if len(self.steered) >= self._expected:
            self.all_steered.set()
        return True


async def test_steer_delivers_peer_note_and_user_upload_in_order_and_flips_wake_polarity(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
    tmp_path: Path,
) -> None:
    agent_id = agent_row(db_conn)
    engine = _BatchSteerEngine(expected=1)
    engine.steering_available = True
    state = AgentState(
        non_human_wake=True,
        harness_pending_input={"prompt": "work", "key": "initial"},
    )
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
    )
    img_path = tmp_path / "preview.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        turn_task = tasks.create_task(harness_turn_node(state, rt, config(agent_id)))
        await engine.running.wait()
        peer_row = db_conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, 'peer progress report', 'chat', 'agent:7') RETURNING id",
            (agent_id,),
        ).fetchone()
        note_row = db_conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, 'subtask updated', 'system_note', 'system', %s::jsonb) RETURNING id",
            (agent_id, json.dumps({"note_tag": "task", "task_id": 99})),
        ).fetchone()
        user_row = db_conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, 'add start/stop buttons', 'chat', 'user', %s::jsonb) RETURNING id",
            (
                agent_id,
                json.dumps(
                    {
                        "image_uploads": [
                            {
                                "name": "preview.png",
                                "size": 8,
                                "content_type": "image/png",
                                "path": str(img_path),
                                "batch_id": "b" * 32,
                                "ordinal": 0,
                            }
                        ]
                    }
                ),
            ),
        ).fetchone()
        db_conn.commit()
        assert peer_row is not None and note_row is not None and user_row is not None
        peer_id, user_id = peer_row[0], user_row[0]
        await engine.all_steered.wait()
        engine.continue_work.set()
        engine.terminal.set()

    command = turn_task.result()
    assert is_object_dict(command.update)
    assert command.update.get("non_human_wake") is False
    assert [key for _, key in engine.steered] == [f"inbound-{user_id}"]
    steered_prompt = engine.steered[0][0]
    peer_pos = steered_prompt.index("peer progress report")
    note_pos = steered_prompt.index("subtask updated")
    user_pos = steered_prompt.index("add start/stop buttons")
    assert peer_pos < note_pos < user_pos
    assert str(img_path) in steered_prompt

    async with aops_pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, agent_id)
    steered_kinds = [(item.kind, item.source, item.inbound_id) for item in items[:3]]
    assert steered_kinds == [
        ("inbound_chat", "agent:7", peer_id),
        ("system_marker", "task", None),
        ("inbound_chat", "user", user_id),
    ]
    events = await live_events.events()
    assert [event["inbound_id"] for event in events if event["role"] == "inbound_committed"] == [
        peer_id,
        user_id,
    ]


class _SteerTimeoutPool(AsyncConnectionPool):
    def __init__(self, delegate: AsyncConnectionPool) -> None:
        super().__init__(open=False, kwargs={**PG_KEEPALIVE_KWARGS})
        self.delegate = delegate
        self.failed = asyncio.Event()
        self.attempts = 0

    @asynccontextmanager
    async def connection(
        self, timeout: float | None = None
    ) -> AsyncGenerator[psycopg.AsyncConnection[TupleRow]]:
        task = asyncio.current_task()
        if task is not None and task.get_name().startswith("harness-steer-"):
            self.attempts += 1
            if self.attempts <= 2:
                self.failed.set()
                raise PoolTimeout("injected steer borrow timeout")
        async with self.delegate.connection(timeout=timeout) as conn:
            yield conn


async def test_steer_pool_timeout_preserves_foreground_and_delivers_once_after_recovery(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = agent_row(db_conn)
    pool = _SteerTimeoutPool(aops_pool)
    engine = _BatchSteerEngine(expected=1)
    engine.steering_available = True
    rt = runtime(
        engine,
        ops_pool=pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
    )
    state = AgentState(harness_pending_input={"prompt": "work", "key": "initial"})
    row = db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, 'keep the logs', 'chat', 'user') RETURNING id",
        (agent_id,),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    inbound_id = row[0]
    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        turn = tasks.create_task(harness_turn_node(state, rt, config(agent_id)))
        await pool.failed.wait()
        assert not turn.done()
        assert engine.steered == []
        assert db_conn.execute(
            "SELECT status, claimed_at FROM inbound_messages WHERE id=%s", (inbound_id,)
        ).fetchone() == ("pending", None)
        await engine.all_steered.wait()
        assert not turn.done()
        engine.continue_work.set()
        engine.terminal.set()

    assert pool.attempts >= 3
    assert [key for _, key in engine.steered] == [f"inbound-{inbound_id}"]
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (inbound_id,)
    ).fetchone() == ("done",)
    async with aops_pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, agent_id)
    assert [item.inbound_id for item in items if item.kind == "inbound_chat"] == [inbound_id]
    events = await live_events.events()
    assert [event["inbound_id"] for event in events if event["role"] == "inbound_committed"] == [
        inbound_id
    ]
    assert events[-1]["role"] == "llm_done"


class _LostSteerEngine(_Foreground):
    async def steer(self, text: str, *, key: str) -> bool:
        self.control_received.set()
        await self.continue_work.wait()
        raise InputLostError("turn ended before the steer receipt arrived")


async def test_lost_steer_returns_to_the_queue_without_an_uncertainty_marker(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = agent_row(db_conn)
    engine = _LostSteerEngine()
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
        engine.continue_work.set()
        engine.terminal.set()

    assert db_conn.execute(
        "SELECT status, claimed_at FROM inbound_messages WHERE id=%s", (inbound_id,)
    ).fetchone() == ("pending", None)
    async with aops_pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, agent_id)
    assert [item.kind for item in items if item.inbound_id == inbound_id] == []
    assert [e for e in await live_events.events() if e["role"] == "inbound_committed"] == []
