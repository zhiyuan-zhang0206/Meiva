# meiva:note
# The stop button's passive note remains deliverable after confirmation. It rides in the next
# input as text; Meiva lifecycle hints do not invent rows in the external record timeline.
# It also holds how a harness session killed by the host's own graceful exit settles its turn,
# which is a restart's interrupt, not a crash.
# meiva:link agent/graph/docs/turn-interrupts.md

from __future__ import annotations

import asyncio
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_interrupt import TimelineNotifications, announce_interrupt
from agent.graph.claim.harness_read_sink import ReadTimelineSink
from agent.graph.claim.harness_turn import harness_turn_node, run_harness_turn
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    completed,
    in_a_turn,
    input_key,
    insert_row,
    publications,
    read_recorded_items,
    row_status,
    silent_engine,
    wait_until,
)
from agent.graph.claim.tests.harness_support import harness_runtime as harness_runtime
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.graph.llm_errors import FatalLLMStreamError, HarnessTurnHaltedError
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.history.transcript_timeline.record.harness import reply_key
from base.agents.recovery.breaker import HALT_REASON_QUOTA_EXHAUSTED
from base.agents.tests.langgraph_views import command_update
from base.agents.to_user.delivery.store import publish
from base.api_contracts.user_delivery import PublishMessage, UserDeliveryMode
from base.clock.tests.fakes import clock_over
from base.clock.tests.manual_timers import ManualTimers
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    HarnessInput,
    InputInContext,
    ReasoningDelta,
    SessionFailed,
    TextDelta,
    TurnCompleted,
    TurnFaulted,
    TurnOrigin,
    TurnStarted,
)
from base.lm.harness.engine import HarnessTurn
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


class NoTimeline:
    def note_input_keys(self, keys: Sequence[str]) -> None:
        pass

    def inbound_committed(self, *, human_input: bool = False) -> None:
        pass

    async def on_event(self, event: BridgeEvent) -> None:
        pass

    async def flush_text(self) -> None:
        pass

    async def record_control(self, text: str, *, error: bool = False) -> None:
        pass

    async def finish(
        self,
        turns: Sequence[HarnessTurn] = (),
        *,
        interrupted: bool = False,
        error: str | None = None,
        input_keys: Sequence[str] = (),
    ) -> None:
        pass


@pytest.mark.parametrize("streamed", [False, True])
async def test_prefix_correction_is_sent_without_a_timeline(
    db_conn: psycopg.Connection, harness_runtime: RuntimeFixture, *, streamed: bool
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "work")

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = [item.key for item in inputs]
        if keys[0].endswith(":visibility-correction"):
            return in_a_turn(keys, "MEIVA_DISPLAY_TO_USER\nDone.", turn_id="t2")
        if streamed:
            return in_a_turn(keys, "", activity=[AssistantText("t1", "Unmarked reply.")])
        return in_a_turn(keys, "Unmarked reply.")

    engine = FakeEngine(script=script)
    ctx = harness_runtime(engine, agent_id=agent_id).context
    publisher = ctx.event_publisher
    assert publisher is not None
    command = await run_harness_turn(
        AgentState(non_human_wake=True),
        ctx,
        agent_id,
        publisher,
        timeline=NoTimeline(),
    )
    assert command_update(command)["non_human_wake"] is False
    assert row_status(db_conn, row) == "done"
    assert [[item.key for item in batch] for batch in engine.delivered] == [
        [input_key(row)],
        [f"{input_key(row)}:visibility-correction"],
    ]


@pytest.mark.parametrize(
    ("host_exiting", "activity", "timeline"),
    [
        (False, False, NoTimeline()),
        (False, True, NoTimeline()),
        (True, False, NoTimeline()),
        (True, True, NoTimeline()),
        (True, True, None),
    ],
)
async def test_session_cut_uses_stream_activity_without_a_timeline(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
    loguru_records: LogRecords,
    *,
    host_exiting: bool,
    activity: bool,
    timeline: NoTimeline | None,
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "work")

    def script(_inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        failure = SessionFailed("child exited with status 143")
        return (TextDelta("t1", "Partial output"), failure) if activity else (failure,)

    class CutEngine(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            if host_exiting:
                _ = insert_row(db_conn, agent_id, "restart", "system:restart", "")
            await super().deliver(inputs, compaction_note=compaction_note)

    engine = CutEngine(script=script)
    ctx = replace(
        harness_runtime(engine, agent_id=agent_id).context, host_exiting=lambda: host_exiting
    )
    engine.record = None
    publisher = ctx.event_publisher
    assert publisher is not None
    if host_exiting:
        command = await run_harness_turn(AgentState(), ctx, agent_id, publisher, timeline=timeline)
        assert command.goto == "claim"
        assert command_update(command)["interrupted_by_restart"] is True
    else:
        with pytest.raises(FatalLLMStreamError, match="child exited with status 143"):
            _ = await run_harness_turn(AgentState(), ctx, agent_id, publisher, timeline=timeline)

    assert row_status(db_conn, row) == "done"
    assert bool(loguru_records.events("turn_fault_lost_inbound")) is not activity


@pytest.mark.parametrize("output", [False, True])
async def test_quota_halt_only_hands_back_inputs_before_output_without_a_timeline(
    db_conn: psycopg.Connection, harness_runtime: RuntimeFixture, *, output: bool
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "work")
    partial = TextDelta("t1", "Some output") if output else ReasoningDelta("t1", "Thinking")
    engine = FakeEngine(script=lambda _inputs: [partial, TurnFaulted("t1", "Rate limit exceeded")])
    ctx = harness_runtime(engine, agent_id=agent_id).context
    publisher = ctx.event_publisher
    assert publisher is not None

    with pytest.raises(HarnessTurnHaltedError) as halted:
        _ = await run_harness_turn(AgentState(), ctx, agent_id, publisher, timeline=NoTimeline())

    assert halted.value.reason == HALT_REASON_QUOTA_EXHAUSTED
    assert halted.value.requeued_inbound_ids == (() if output else (row,))
    assert row_status(db_conn, row) == ("done" if output else "pending")


async def test_a_completed_reply_without_a_record_fails_even_without_a_timeline(
    db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "work")
    engine = FakeEngine()
    ctx = harness_runtime(engine, agent_id=agent_id).context
    engine.record = None
    publisher = ctx.event_publisher
    assert publisher is not None

    with pytest.raises(
        FatalLLMStreamError, match=r"record of the antigravity session .* was not found"
    ):
        _ = await run_harness_turn(AgentState(), ctx, agent_id, publisher, timeline=NoTimeline())
    assert row_status(db_conn, row) == "done"


@pytest.mark.parametrize("real_timeline", [False, True])
async def test_terminal_projection_can_wait_while_correction_and_next_input_are_delivered(
    db_conn: psycopg.Connection, harness_runtime: RuntimeFixture, *, real_timeline: bool
) -> None:
    agent_id = agent_row(db_conn)
    first = insert_row(db_conn, agent_id, "chat", "user", "first request")
    first_key = input_key(first)
    correction_key = f"{first_key}:visibility-correction"
    projection_polling = asyncio.Event()
    release_projection = asyncio.Event()

    class ClosingTimers(ManualTimers):
        async def asleep(self, seconds: float) -> None:
            projection_polling.set()
            _ = await release_projection.wait()
            await super().asleep(seconds)

    class LateRecordEngine(FakeEngine):
        def emit(self, *events: BridgeEvent) -> None:
            if len(self.delivered) != 1:
                super().emit(*events)
                return
            record = self.record
            assert record is not None
            for event in events:
                if not isinstance(event, (AssistantText, TurnCompleted)):
                    record.observe(event)
            for subscription in tuple(self.subscriptions):
                for event in events:
                    subscription.queue.put_nowait(event)

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = [item.key for item in inputs]
        if keys == [first_key]:
            return in_a_turn(keys, "Unmarked reply.", turn_id="first")
        if keys == [correction_key]:
            return []
        return in_a_turn(keys, "MEIVA_DISPLAY_TO_USER\nSecond reply.", turn_id="second")

    engine = LateRecordEngine(script=script)
    ctx = harness_runtime(engine, agent_id=agent_id).context
    publisher = ctx.event_publisher
    session = engine.session_id
    assert publisher is not None and session is not None
    clock = clock_over(ClosingTimers())
    timeline = (
        ReadTimelineSink(
            replace(ctx, clock_factory=lambda: clock),
            agent_id,
            publisher,
            non_human_wake=False,
            session_id=lambda: session,
        )
        if real_timeline
        else NoTimeline()
    )

    async def next_input() -> int:
        if real_timeline:
            _ = await projection_polling.wait()
        await engine.wait_deliveries(2)
        assert engine.delivered[1][0].key == correction_key
        second = insert_row(db_conn, agent_id, "chat", "user", "second request")
        engine.notify_inbound()
        await engine.wait_deliveries(3)
        engine.emit(
            *in_a_turn([correction_key], "MEIVA_DISPLAY_TO_USER\nCorrected.", turn_id="correction")
        )
        release_projection.set()
        return second

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        later = tasks.create_task(next_input())
        command = await run_harness_turn(AgentState(), ctx, agent_id, publisher, timeline=timeline)

    second = later.result()
    assert command.goto == "claim"
    assert [[item.key for item in batch] for batch in engine.delivered] == [
        [first_key],
        [correction_key],
        [input_key(second)],
    ]
    assert row_status(db_conn, first) == row_status(db_conn, second) == "done"


def _seed_reply_history(
    db_conn: psycopg.Connection, agent_id: int, created_at: datetime, *, other_session: bool
) -> list[str]:
    prior = [("assistant:old-publication", "Previous reply.")]
    if other_session:
        key = reply_key("antigravity", "agy:0", "a-prior-session")
        prior.append((key, "Prior session's canonical reply."))
    for key, content in prior:
        _ = publish(
            db_conn,
            agent_id,
            key,
            PublishMessage(content=content, suggestions=[]),
            mode=UserDeliveryMode.PREFIX,
            created_at=created_at,
        )
    db_conn.commit()
    return [content for _, content in prior]


@contextmanager
def _unreadable(path: Path, denied_path: Path, mode: int) -> Generator[None]:
    denied_path.chmod(0o000)
    try:
        try:
            _ = path.read_bytes()
        except PermissionError:
            denied = True
        else:
            denied = False
        if not denied:
            pytest.skip("the test process can read files with mode 000")
        yield
    finally:
        denied_path.chmod(mode)


@pytest.mark.parametrize("real_timeline", [False, True])
@pytest.mark.parametrize(
    ("restore_readability", "stat_available", "other_session"),
    [(False, True, False), (True, True, True), (True, False, True), (True, False, False)],
)
async def test_unreadable_transcript_preserves_execution_and_publication_floor(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
    loguru_records: LogRecords,
    *,
    real_timeline: bool,
    restore_readability: bool,
    stat_available: bool,
    other_session: bool,
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "work")

    class Engine(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            if restore_readability:
                denied_path.chmod(mode)
            await super().deliver(inputs, compaction_note=compaction_note)

    engine = Engine()
    ctx = harness_runtime(engine, agent_id=agent_id).context
    record = engine.record
    publisher = ctx.event_publisher
    assert record is not None and publisher is not None
    record.write("PLANNER_RESPONSE", content="MEIVA_DISPLAY_TO_USER\nPrevious reply.")
    path = record.path
    expected = _seed_reply_history(
        db_conn, agent_id, ctx.require_clock().now(), other_session=other_session
    )
    if not restore_readability:
        engine.record = None
    denied_path, mode = (path, 0o600) if stat_available else (path.parent, 0o700)
    with _unreadable(path, denied_path, mode):
        command = await run_harness_turn(
            AgentState(), ctx, agent_id, publisher, timeline=None if real_timeline else NoTimeline()
        )

    assert command.goto == "claim"
    assert [key for _, key in engine.submitted] == [input_key(row)]
    assert row_status(db_conn, row) == "done"
    assert bool(loguru_records.events("harness_live_timeline_failed")) == real_timeline
    if real_timeline and restore_readability:
        expected.append("the answer")
    assert [content for _, content in publications(db_conn, agent_id)] == expected
    if real_timeline:
        sink = ReadTimelineSink(
            ctx, agent_id, publisher, non_human_wake=False, session_id=lambda: record.session
        )
        await sink.finish()
        assert [content for _, content in publications(db_conn, agent_id)] == expected


@pytest.mark.parametrize("scenario", ["normal", "abort", "graceful_exit"])
async def test_a_blocked_closing_row_poll_cannot_hold_turn_exit(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
    loguru_records: LogRecords,
    scenario: str,
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "work")
    polling = asyncio.Event()
    stop_poll = asyncio.Event()

    class PollTimers(ManualTimers):
        async def asleep(self, seconds: float) -> None:
            polling.set()
            _ = await stop_poll.wait()

    class Engine(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            if scenario == "graceful_exit":
                _ = insert_row(db_conn, agent_id, "restart", "system:restart", "")
            await super().deliver(inputs, compaction_note=compaction_note)

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        if scenario == "normal":
            return in_a_turn([item.key for item in inputs], "")
        return [TextDelta("t1", "Partial"), SessionFailed("child exited 143")]

    engine = Engine(script=script)
    ctx = replace(
        harness_runtime(engine, agent_id=agent_id).context,
        host_exiting=lambda: scenario == "graceful_exit",
    )
    publisher = ctx.event_publisher
    assert publisher is not None
    clock = clock_over(PollTimers())
    sink = ReadTimelineSink(
        replace(ctx, clock_factory=lambda: clock),
        agent_id,
        publisher,
        non_human_wake=False,
        session_id=lambda: engine.session_id,
    )
    work = run_harness_turn(AgentState(), ctx, agent_id, publisher, timeline=sink)
    async with asyncio.timeout(4):
        if scenario == "abort":
            with pytest.raises(FatalLLMStreamError, match="child exited 143"):
                _ = await work
        else:
            command = await work
            assert command.goto == "claim"
            assert command_update(command).get("interrupted_by_restart", False) == (
                scenario == "graceful_exit"
            )

    assert polling.is_set()
    assert row_status(db_conn, row) == "done"
    assert not any(t.get_name() == f"harness-projection-{agent_id}" for t in asyncio.all_tasks())
    events = loguru_records.extras(event="harness_live_timeline_failed")
    assert [event.get("reason") for event in events] == ["timeout"]


async def test_cancelled_projection_publishes_the_terminal_notification_and_reports_drain(
    loguru_records: LogRecords,
) -> None:
    agent_id = 91235
    projection = TimelineNotifications(agent_id, NoTimeline())
    active = asyncio.Event()
    terminal: list[str] = []

    async def blocked_effect() -> None:
        active.set()
        _ = await asyncio.Event().wait()

    async def invocation() -> None:
        async with projection.running():
            projection.post(blocked_effect)
            projection.post_terminal(lambda: terminal.append("done"))
            _ = await asyncio.Event().wait()

    task = asyncio.create_task(invocation())
    try:
        async with asyncio.timeout(4):
            _ = await active.wait()
    finally:
        _ = task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert terminal == ["done"]
    assert not any(t.get_name() == f"harness-projection-{agent_id}" for t in asyncio.all_tasks())
    (event,) = loguru_records.extras(event="harness_live_timeline_failed")
    assert event["reason"] == "cancelled"
    assert event["queued_notifications"] == 1
