from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.runtime import Runtime
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_activity import HarnessTurnProjector, log_turn_usage
from agent.graph.claim.harness_interrupt import interrupt_engine
from agent.graph.claim.harness_render import pending_messages, render_messages
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.lost_turn import lost_turn_note, report_lost_turn
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    config,
    read_recorded_items,
    runtime,
)
from agent.graph.claim.tests.harness_support import (
    harness_runtime as harness_runtime,
)
from agent.graph.claim.tests.harness_support import hosted_resources as hosted_resources
from agent.graph.llm_errors import FatalLLMStreamError
from agent.messages import NoteTag
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import format_tool_use
from base.agents.messages.kwargs import read_meiva_kwargs
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.lm.catalog import ModelCatalog
from base.lm.harness import (
    AssistantText,
    HarnessInput,
    HarnessSetupError,
    HarnessTurn,
    InputInContext,
    ReasoningDelta,
    ReasoningText,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnFaulted,
    TurnOrigin,
    TurnStarted,
    Usage,
)
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records


def _last_message_text(db: psycopg.Connection, agent_id: int) -> str | None:
    row = db.execute(
        "SELECT last_message_text FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def inbound(text: str, inbound_id: int) -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs={"meiva_msg_type": "inbound", "meiva_inbound_id": inbound_id},
    )


def test_own_replies_are_never_pending() -> None:
    messages = [
        inbound("hello", 11),
        AIMessage(content="hi"),
        ToolMessage(content="out", tool_call_id="call_1"),
    ]
    assert [message.content for message in pending_messages(messages)] == ["hello"]


def test_multimodal_content_renders_its_text_and_marks_the_rest() -> None:
    message = HumanMessage(
        content=[
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]
    )

    assert render_messages([message]) == "look\n[image_url]"


async def test_a_turn_submits_the_pending_messages_and_commits_the_reply(
    db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)
    engine = FakeEngine(reply="MEIVA_DISPLAY_TO_USER\ndone")
    state = AgentState(
        harness_inputs=[{"key": "inbound-7", "prompt": "do it", "timeline_items": []}]
    )
    command = await harness_turn_node(
        state, harness_runtime(engine, agent_id=agent_id), config(agent_id)
    )
    assert command.goto == "claim"
    assert command.update is not None
    [(prompt, key)] = engine.submitted
    assert prompt == "do it"
    assert key == "inbound-7"
    assert command.update["harness_inputs"] == []
    assert command.update["halted"] is True


async def test_a_turn_with_nothing_pending_ends_without_calling_the_engine(
    db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)
    engine = FakeEngine()
    state = AgentState()
    command = await harness_turn_node(
        state, harness_runtime(engine, agent_id=agent_id), config(agent_id)
    )
    assert command.goto == "claim"
    assert engine.submitted == []
    assert command.update == {"halted": True, "harness_inputs": [], "harness_view": []}


async def test_a_turn_logs_usage_under_the_harness(
    loguru_records: LogRecords, db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)
    engine = FakeEngine(usage=Usage(input_tokens=1200, output_tokens=34))
    state = AgentState(
        harness_inputs=[{"key": "inbound-7", "prompt": "do it", "timeline_items": []}]
    )
    await harness_turn_node(state, harness_runtime(engine, agent_id=agent_id), config(agent_id))
    usage = loguru_records.events("llm_usage")
    assert len(usage) == 1
    extras = usage[0]["extra"]
    assert extras["source"] == "antigravity"
    assert extras["in_total"] == 1200
    assert extras["model"] == "claude-sonnet-5-5"
    assert extras["usage_kind"] == "agent"


# meiva:note
# Usage accounting is best effort: the turn has already finished when it is logged, so
# an unpriced bill must leave a hole in a row, never take the reply with it. The usage
# here carries more cache reads than prompt tokens, which is exactly what `quote`
# rejects — the shape a provider produces when the adapter misreports its fields.
# meiva:link agent/graph/docs/harness-turn.md
async def test_an_unpriceable_usage_keeps_the_reply_and_reports_the_gap(
    loguru_records: LogRecords, db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)
    engine = FakeEngine(
        reply="MEIVA_DISPLAY_TO_USER\ndone",
        usage=Usage(input_tokens=8, output_tokens=3, cache_read_input_tokens=99),
    )
    state = AgentState(
        harness_inputs=[{"key": "inbound-7", "prompt": "do it", "timeline_items": []}]
    )
    command = await harness_turn_node(
        state, harness_runtime(engine, agent_id=agent_id), config(agent_id)
    )
    assert command.update is not None
    assert command.update["harness_inputs"] == []
    assert command.update["halted"] is True
    warned = loguru_records.events("harness_usage_log_failed")
    assert len(warned) == 1
    assert warned[0]["extra"]["agent_id"] == agent_id
    assert loguru_records.events("llm_usage") == []


async def test_a_faulted_session_settles_the_turn_as_a_fatal_stream_abort(
    loguru_records: LogRecords, db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)

    class FaultingEngine(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            self.emit(TurnFaulted("t1", "cli died"))

    state = AgentState(
        harness_inputs=[{"key": "inbound-7", "prompt": "do it", "timeline_items": []}]
    )
    with pytest.raises(FatalLLMStreamError, match="antigravity harness turn aborted: cli died"):
        await harness_turn_node(
            state, harness_runtime(FaultingEngine(), agent_id=agent_id), config(agent_id)
        )
    assert loguru_records.events("llm_usage") == [], "an aborted turn logs no usage"


# meiva:note
# A session that never started is not a crash: an agent whose provider CLI is missing
# or whose submission was refused settles the turn with a reported abort and idles for
# the next inbound, the same way a dead model stream does. Before this, the exception
# escaped the node and cost the host a crash per wake.
# meiva:link agent/graph/docs/harness-turn.md
async def test_a_session_that_cannot_start_settles_the_turn_as_a_fatal_stream_abort(
    db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)

    class UnstartableEngine(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            raise HarnessSetupError("antigravity session could not start: no sdk")

    state = AgentState(
        harness_inputs=[{"key": "inbound-7", "prompt": "do it", "timeline_items": []}]
    )
    with pytest.raises(FatalLLMStreamError, match="harness turn aborted"):
        await harness_turn_node(
            state, harness_runtime(UnstartableEngine(), agent_id=agent_id), config(agent_id)
        )


class SettledReport:
    def __init__(self, settled: bool | None) -> None:
        self.settled = settled
        self.turn_id = "t1"


class InterruptingEngine(FakeEngine):
    def __init__(self, report: object = None) -> None:
        super().__init__()
        self._report = report
        self.interrupts = 0

    async def interrupt(self) -> object:
        self.interrupts += 1
        if isinstance(self._report, Exception):
            raise self._report
        return self._report


async def test_the_interrupt_report_is_logged_as_it_came_back(loguru_records: LogRecords) -> None:
    await interrupt_engine(6984, InterruptingEngine(SettledReport(settled=None)))

    logged = loguru_records.events("harness_interrupt")
    assert len(logged) == 1
    assert logged[0]["extra"]["settled"] is None, "unobservable is reported, not rounded to reaped"


async def test_an_interrupt_that_raises_is_reported_not_raised(
    loguru_records: LogRecords,
) -> None:
    engine = InterruptingEngine(RuntimeError("client gone"))

    await interrupt_engine(6984, engine)

    assert engine.interrupts == 1
    assert len(loguru_records.events("harness_interrupt_failed")) == 1


# meiva:note
# `meiva.agents.get_last_message()` reads `agents_meta.last_message_text`, which only the
# native LLM node used to write — every harness agent therefore answered `None` for its
# whole life while its transcript held the text (#5511, measured on codex / claude /
# antigravity agents on 2026-10-21). The harness node now stamps it where the native one
# does, so the reader needs no branch of its own.
# meiva:link agent/graph/docs/harness-turn.md
async def test_a_completed_turn_stamps_the_reply_as_the_last_message(
    redis_url: str,
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    engine = FakeEngine(reply="MEIVA_DISPLAY_TO_USER\nthe answer")
    await harness_turn_node(
        AgentState(harness_inputs=[{"key": "inbound-7", "prompt": "do it", "timeline_items": []}]),
        harness_runtime(engine, agent_id=agent_id),
        config(agent_id),
    )
    assert _last_message_text(db_conn, agent_id) == "the answer"


async def test_tool_activity_is_visible_before_completion_and_survives_abort(
    redis_url: str,
    aops_pool: AsyncConnectionPool,
    harness_runtime: RuntimeFixture,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )

    async def load() -> list[TimelineItem]:
        async with aops_pool.connection() as conn:
            return await read_recorded_items(conn, agent_id)

    class FaultingEngine(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            self.emit(
                ToolUse(
                    turn_id="t1", tool_use_id="call", name="Bash", tool_input={"command": "ls"}
                ),
                ToolResult(turn_id="t1", tool_use_id="call", content="README", is_error=False),
                TurnFaulted("t1", "provider disconnected"),
            )

    state = AgentState(
        harness_inputs=[{"key": "inbound-7", "prompt": "list files", "timeline_items": []}]
    )
    with pytest.raises(FatalLLMStreamError, match="provider disconnected"):
        await harness_turn_node(
            state, harness_runtime(FaultingEngine(), agent_id=agent_id), config(agent_id)
        )
    result = await load()
    assert [item.payload for item in result] == ["ls", "README"]


async def test_intra_turn_events_project_to_live_sse_and_committed_messages(
    live_events: LiveEvents,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    engine = FakeEngine(
        reply="MEIVA_DISPLAY_TO_USER\nall done",
        events=(
            TextDelta(turn_id="t1", text="MEIVA_HIDE_FROM_USER\nChecking "),
            TextDelta(turn_id="t1", text="files..."),
            ToolUse(
                turn_id="t1", tool_use_id="call_1", name="Bash", tool_input={"command": "ls -la"}
            ),
            ToolResult(turn_id="t1", tool_use_id="call_1", content="README.md\n", is_error=False),
            TextDelta(turn_id="t1", text="MEIVA_DISPLAY_TO_USER\nall done"),
        ),
    )
    state = AgentState(
        harness_inputs=[{"key": "inbound-7", "prompt": "list files", "timeline_items": []}]
    )
    command = await harness_turn_node(
        state,
        harness_runtime(engine, publisher=live_events.publisher, agent_id=agent_id),
        config(agent_id),
    )
    assert command.update == {"halted": True, "harness_inputs": [], "harness_view": []}
    events = await live_events.events()
    assert events[-1]["role"] == "llm_done"
    latest = {item["item_id"]: item for event in events for item in event.get("items", [])}
    assert [(item["kind"], item["payload"]) for item in latest.values()] == [
        ("agent_code", "ls -la"),
        ("code_output", "README.md"),
        ("agent_chat", "all done"),
    ]
    async with aops_pool.connection() as conn:
        stored = await read_recorded_items(conn, agent_id)
    assert {item.item_id: item.payload for item in stored} == {
        key: item["payload"] for key, item in latest.items()
    }


async def test_consecutive_assistant_text_blocks_project_to_distinct_ai_messages(
    live_events: LiveEvents, db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)
    engine = FakeEngine(
        reply="MEIVA_DISPLAY_TO_USER\nSecond message",
        events=(
            TextDelta(turn_id="t1", text="MEIVA_H"),
            TextDelta(turn_id="t1", text="IDE_FROM_USER\nFirst "),
            AssistantText(turn_id="t1", text="MEIVA_HIDE_FROM_USER\nFirst message"),
            TextDelta(turn_id="t1", text="MEIVA_DISPLAY_TO_USER\n"),
            TextDelta(turn_id="t1", text="Second "),
            AssistantText(turn_id="t1", text="MEIVA_DISPLAY_TO_USER\nSecond message"),
        ),
    )
    state = AgentState(
        harness_inputs=[{"key": "inbound-7", "prompt": "two steps", "timeline_items": []}]
    )
    command = await harness_turn_node(
        state,
        harness_runtime(engine, publisher=live_events.publisher, agent_id=agent_id),
        config(agent_id),
    )
    assert command.update == {"halted": True, "harness_inputs": [], "harness_view": []}
    events = await live_events.events()
    latest = {item["item_id"]: item for event in events for item in event.get("items", [])}
    assert [(item["payload"], item["user_facing"]) for item in latest.values()] == [
        ("First message", False),
        ("Second message", True),
    ]
    assert len(set(latest)) == 2


def test_format_tool_use_renders_patch_diffs_and_paths() -> None:
    rendered = format_tool_use(
        "patch",
        {
            "paths": ["demo.py"],
            "changes": [
                {
                    "path": "demo.py",
                    "kind": "add",
                    "diff": "@@ -0,0 +1,2 @@\n+print('hi')\n",
                }
            ],
        },
    )
    assert rendered == "# add: demo.py\n@@ -0,0 +1,2 @@\n+print('hi')"
    assert format_tool_use("patch", {"paths": ["a.py", "b.py"]}) == "a.py\nb.py"


async def test_multi_step_turn_logs_each_step_usage_and_calls(
    loguru_records: LogRecords,
) -> None:
    engine = FakeEngine()
    ctx = runtime(engine).context
    turn = HarnessTurn(
        turn_id="t1",
        text="done",
        usage=Usage(input_tokens=300, output_tokens=30),
        cost_usd=None,
        subtype="completed",
        is_error=False,
        interrupted=False,
        num_turns=2,
        steps=(
            Usage(input_tokens=100, output_tokens=10),
            Usage(input_tokens=200, output_tokens=20),
        ),
    )
    log_turn_usage(ctx, 6984, "claude-fable-5-1[1m]", turn)
    rows = loguru_records.events("llm_usage")
    assert len(rows) == 2
    assert [row["extra"]["in_total"] for row in rows] == [100, 200]
    assert [row["extra"]["calls"] for row in rows] == [1, 1]
    assert [row["extra"]["model"] for row in rows] == [
        "claude-fable-5-1",
        "claude-fable-5-1",
    ]
    assert all("unpriced" not in row["extra"] for row in rows)


async def test_harness_turn_projects_recorded_reasoning_tools_and_reply(
    live_events: LiveEvents, db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)
    engine = FakeEngine(
        events=(
            ReasoningDelta(turn_id="t1", text="Planning "),
            ReasoningDelta(turn_id="t1", text="tool call"),
            ReasoningText(turn_id="t1", text="Planning tool call"),
            ToolUse(
                turn_id="t1", tool_use_id="call-1", name="Bash", tool_input={"command": "echo ok"}
            ),
            ToolResult(turn_id="t1", tool_use_id="call-1", is_error=False, content="ok\n"),
            ReasoningText(turn_id="t1", text="Synthesizing reply"),
            AssistantText(turn_id="t1", text="MEIVA_DISPLAY_TO_USER\nFinal answer"),
        )
    )
    state = AgentState(
        harness_inputs=[{"key": "inbound-9", "prompt": "run it", "timeline_items": []}]
    )
    command = await harness_turn_node(
        state,
        harness_runtime(engine, publisher=live_events.publisher, agent_id=agent_id),
        config(agent_id),
    )
    assert command.update == {"halted": True, "harness_inputs": [], "harness_view": []}
    events = await live_events.events()
    latest = {item["item_id"]: item for event in events for item in event.get("items", [])}
    assert [(item["kind"], item["payload"]) for item in latest.values()] == [
        ("agent_reasoning", "Planning tool call"),
        ("agent_code", "echo ok"),
        ("code_output", "ok"),
        ("agent_reasoning", "Synthesizing reply"),
        ("agent_chat", "Final answer"),
    ]


async def test_terminate_interrupt_preserves_projected_tool_call_messages(
    db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:
    agent_id = agent_row(db_conn)
    from agent.graph.interrupt import ModelInterruptedError
    from base.agents.messages.inbound import InterruptReason

    class SelfTerminatingEngine(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            self.delivered.append(list(inputs))
            self.emit(
                ReasoningText(turn_id="t1", text="Role complete, terminating."),
                ToolUse(
                    turn_id="t1",
                    tool_use_id="call-term",
                    name="Bash",
                    tool_input={"command": "python3 -c 'import meiva; meiva.self.terminate()'"},
                ),
            )
            raise ModelInterruptedError(InterruptReason.SYSTEM)

    engine = SelfTerminatingEngine()
    state = AgentState(
        harness_inputs=[
            {"key": "window-4", "prompt": "[Heartbeat]: check in", "timeline_items": []}
        ]
    )
    command = await harness_turn_node(
        state, harness_runtime(engine, agent_id=agent_id), config(agent_id)
    )
    [(prompt, key)] = engine.submitted
    assert prompt == "[Heartbeat]: check in"
    assert key == "window-4"
    assert command.update is not None
    assert command.update == {"halted": True, "harness_inputs": [], "harness_view": []}


# meiva:note
# The note a lost turn leaves behind has to say three things and no fourth: that the message was not
# answered, which rows were consumed, and that they will not come back. It is a system note, not a
# reply, so a reader cannot mistake it for the agent having answered (#6150).
def test_the_lost_turn_note_says_it_is_not_a_reply_and_never_returns() -> None:
    now = datetime(2026, 11, 4, 2, 11, tzinfo=UTC)

    note = lost_turn_note(
        [7379],
        "turn-1",
        "HarnessTurnFaultedError: antigravity turn turn-1 failed (API 400): boom",
        now=now,
    )
    kwargs = read_meiva_kwargs(note)
    content = str(note.content)

    assert kwargs.get("meiva_msg_type") == "system_note"
    assert kwargs.get("meiva_note_tag") == NoteTag.TURN_LOST.value
    assert "was not answered" in content
    assert "7379" in content
    assert "will not deliver it again" in content
    assert "API 400" in content


async def test_the_projector_reports_whether_the_turn_produced_activity() -> None:
    async def checkpoint(item: TimelineItem, msg_count: int) -> None:
        pass

    clock = MagicMock()
    clock.now.return_value = datetime(2026, 11, 6, tzinfo=UTC)
    projector = HarnessTurnProjector(checkpoint=checkpoint, base_idx=0, clock=clock)
    assert projector.has_activity is False
    await projector.on_event(AssistantText(turn_id="t1", text="hello"))
    assert projector.has_activity is True


# meiva:note
# The record has to exist, not merely be attempted: this asserts the note reached the append path
# and that a turn which produced text appends nothing.
async def test_a_lost_turn_appends_one_note_and_a_quiet_turn_appends_none() -> None:
    now = datetime(2026, 11, 4, 2, 11, tzinfo=UTC)
    ctx = MagicMock()
    ctx.require_clock.return_value.now.return_value = now
    ctx.harness.value = "antigravity"
    appended: list[list[BaseMessage]] = []

    async def append(_ctx: object, _agent_id: int, messages: Sequence[BaseMessage]) -> None:
        appended.append(list(messages))

    await report_lost_turn(
        ctx,
        7,
        [7379],
        "turn-1",
        RuntimeError("boom"),
        append=append,
    )

    assert len(appended) == 1
    assert read_meiva_kwargs(appended[0][0]).get("meiva_note_tag") == NoteTag.TURN_LOST.value
    assert "7379" in str(appended[0][0].content)


# meiva:note
# The call that never ends: the agent asked to terminate, and the CLI's own model request cannot finish
# (agent 7600, 2026-11-11, three hours of provider retries). Its own terminate is read by the foreground's
# lifecycle watcher once the grace has passed, so the invocation returns to claim, which takes the terminate
# (the host's exit then closes the session). The row is aged instead of waiting out the grace.
# meiva:link agent/graph/docs/turn-interrupts.md
async def test_an_own_terminate_ends_an_invocation_whose_turn_cannot_finish(
    db_conn: psycopg.Connection, harness_runtime: RuntimeFixture
) -> None:

    def insert(kind: str, source: str) -> int:
        row = db_conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) VALUES (%s, '', %s, %s) RETURNING id",
            (agent_id, kind, source),
        ).fetchone()
        db_conn.commit()
        assert row is not None
        return row[0]

    agent_id = agent_row(db_conn)
    key = f"inbound-{insert('chat', 'user')}"
    engine = FakeEngine(script=lambda _inputs: [])
    rt = harness_runtime(engine, agent_id=agent_id)
    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        while not engine.delivered:
            await asyncio.sleep(0.02)
        engine.emit(TurnStarted("t1", TurnOrigin.INPUT, (key,), 1), InputInContext(key, "t1", 1))
        terminate = insert("terminate", "self")
        db_conn.execute(
            "UPDATE inbound_messages SET created_at = clock_timestamp() - interval '1 minute' WHERE id=%s",
            (terminate,),
        )
        db_conn.commit()
        command = await node
    assert command.goto == "claim"
    assert command.update is not None and command.update["halted"] is True
    row = db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (terminate,)
    ).fetchone()
    assert row == ("pending",), "claim dispatches the terminate next"


async def test_after_the_cli_compacted_the_next_input_carries_the_open_questions(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
    harness_runtime: RuntimeFixture,
) -> None:
    agent = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    question = db_conn.execute(
        "INSERT INTO agent_questions (agent_id, title, question) VALUES (%s, 'Which database?', %s) RETURNING id",
        (agent, Jsonb({"type": "text"})),
    ).fetchone()
    assert question is not None
    db_conn.commit()
    state = AgentState(
        harness_inputs=[{"key": "inbound-7", "prompt": "do it", "timeline_items": []}]
    )

    async def notes(compacted: bool) -> list[str]:
        engine = FakeEngine(reply="MEIVA_DISPLAY_TO_USER\ndone")
        engine.context_compacted = compacted
        base = harness_runtime(engine, agent_id=agent)
        context = replace(
            base.context, db=Database.from_settings(gate=database_gate), bus=event_bus
        )
        await harness_turn_node(state, Runtime(context=context), config(agent))
        return engine.compaction_notes

    [note] = await notes(True)
    assert f'#{question[0]} "Which database?"' in note
    assert "Questions you asked the user that are still unanswered" in note
    assert await notes(False) == [""]


# meiva:note
# A wake's pre-turn hook note and the person's message reach the harness as one input, so its first
# model request already holds the message; the compaction question list stays a prefix apart.
# meiva:link agent/graph/docs/harness-turn.md
_HOOK_NOTE = "Skill recommendation: read the `inspect-a-trace` skill."


async def test_a_wake_after_a_compaction_carries_the_questions_and_the_hook_note_with_the_message(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
    harness_runtime: RuntimeFixture,
) -> None:
    agent = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    _ = db_conn.execute(
        "INSERT INTO agent_questions (agent_id, title, question) VALUES (%s, 'Which database?', %s)",
        (agent, Jsonb({"type": "text"})),
    )
    db_conn.commit()
    inserted = db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) VALUES (%s, 'why is the build red?', 'chat', 'user') RETURNING id",
        (agent,),
    ).fetchone()
    assert inserted is not None
    row = inserted[0]
    db_conn.commit()
    engine = FakeEngine()
    engine.context_compacted = True
    note: dict[str, object] = {
        "key": f"hook-notes-{row}",
        "prompt": _HOOK_NOTE,
        "timeline_items": [],
    }
    base = harness_runtime(engine, agent_id=agent)
    context = replace(base.context, db=Database.from_settings(gate=database_gate), bus=event_bus)
    _ = await harness_turn_node(
        AgentState(harness_inputs=[note]), Runtime(context=context), config(agent)
    )
    [batch] = engine.delivered
    assert [item.key for item in batch] == [f"inbound-{row}"], "one input for the wake"
    assert batch[0].text.endswith(f"\n\n{_HOOK_NOTE}")
    assert "why is the build red?" in batch[0].text
    [questions] = engine.compaction_notes
    assert "Which database?" in questions, "the question list is the compaction prefix, apart"
    assert "Which database?" not in batch[0].text
    async with aops_pool.connection() as conn:
        shown = [str(item.payload) for item in await read_recorded_items(conn, agent)]
    assert not any(_HOOK_NOTE in payload for payload in shown), "never shown as the person's"
