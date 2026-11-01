from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_turn import (
    format_tool_use,
    harness_turn_node,
    interrupt_engine,
    log_turn_usage,
    pending_messages,
    render_messages,
    turn_key,
)
from agent.graph.llm_errors import FatalLLMStreamError
from agent.state import AgentState
from base.agents.context import HostedTurnResources, MeivaContext
from base.agents.tests.units import spawn_agent
from base.api_contracts.agent_contract import Harness
from base.clock import Clock
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import defaults
from base.db.code_version_gate import ProcessDbGate
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    HarnessSetupError,
    HarnessTurn,
    HarnessTurnFaultedError,
    ReasoningDelta,
    ReasoningText,
    TextDelta,
    ToolResult,
    ToolUse,
    Usage,
)
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.turn_identity import HostedServiceResources
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home

ENVIRON = dict(os.environ)


class FakeEngine:
    # meiva:note
    # The turn engine as the node sees it: a protocol member or two and one call.
    # Recording the submission is what lets a test say which messages the harness
    # received and under which key, without a provider or a process.
    # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
    def __init__(
        self,
        reply: str = "the answer",
        usage: Usage | None = None,
        prelude: tuple[HarnessTurn, ...] = (),
        events: tuple[BridgeEvent, ...] = (),
    ) -> None:
        self.harness = Harness.CLAUDE_CODE
        self.model = "claude-sonnet-5-5"
        self.exit_code: int | None = None
        self.submitted: list[tuple[str, str]] = []
        self.interrupts = 0
        self.closed = False
        self._reply = reply
        self._usage = usage
        self._prelude = prelude
        self._events = events

    async def run_turn(
        self,
        text: str,
        *,
        key: str,
        on_event: Callable[[BridgeEvent], None] | None = None,
    ) -> HarnessTurn:
        self.submitted.append((text, key))
        if on_event is not None:
            for event in self._events:
                on_event(event)
        return HarnessTurn(
            turn_id="t1",
            text=self._reply,
            usage=self._usage or Usage(input_tokens=7, output_tokens=3),
            cost_usd=0.01,
            subtype="success",
            is_error=False,
            interrupted=False,
            prelude=self._prelude,
        )

    async def interrupt(self) -> object:
        self.interrupts += 1
        return object()

    async def close(self, *, reason: str = "closed") -> None:
        self.closed = True


def runtime(
    engine: FakeEngine | None,
    *,
    ops_pool: AsyncConnectionPool | None = None,
    hosted_resources: HostedTurnResources | None = None,
    publisher: MagicMock | None = None,
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            meiva_home=meiva_home(),
            ops_pool=ops_pool,
            hosted_resources=hosted_resources,
            harness=Harness.CLAUDE_CODE if engine is not None else Harness.MEIVA,
            harness_bridge=engine,
            llm=MagicMock(),
            event_publisher=publisher or MagicMock(),
            agent=AgentSlices.resolve(default_reader=defaults()),
            catalog=build_model_catalog(home=meiva_home()),
            clock_factory=Clock.from_settings,
            environ=ENVIRON,
        )
    )


@pytest.fixture
async def hosted_resources() -> AsyncGenerator[HostedTurnResources]:
    """An explicit service owns the turn's watcher until the test finishes."""
    service = HostedServiceResources()
    try:
        yield await service.turn()
    finally:
        await service.aclose()


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


def config(agent_id: int = 6984) -> RunnableConfig:
    return {"configurable": {"thread_id": str(agent_id)}}


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


def test_the_key_is_the_newest_inbound_row() -> None:
    pending = [inbound("one", 11), HumanMessage(content="note"), inbound("two", 12)]

    assert turn_key(pending) == "inbound-12"


def test_a_notes_only_window_uses_its_length() -> None:
    assert turn_key([HumanMessage(content="note")]) == "window-1"
    assert turn_key([HumanMessage(content="note")], cursor=5) == "window-6"
    assert turn_key([HumanMessage(content="second note")], cursor=7) == "window-8"


async def test_a_turn_submits_the_pending_messages_and_commits_the_reply() -> None:
    engine = FakeEngine(reply="done")
    state = AgentState(harness_pending_input={"prompt": "do it", "key": "inbound-7"})

    command = await harness_turn_node(state, runtime(engine), config())

    assert command.goto == "claim"
    assert command.update is not None
    assert engine.submitted == [("do it", "inbound-7")]
    assert command.update["harness_pending_input"] is None
    assert command.update["halted"] is True


async def test_a_turn_with_nothing_pending_ends_without_calling_the_engine() -> None:
    engine = FakeEngine()
    state = AgentState()

    command = await harness_turn_node(state, runtime(engine), config())

    assert command.goto == "claim"
    assert engine.submitted == []
    assert command.update == {"halted": True, "harness_pending_input": None}


async def test_a_turn_logs_usage_under_the_harness(loguru_records: LogRecords) -> None:
    engine = FakeEngine(usage=Usage(input_tokens=1200, output_tokens=34))
    state = AgentState(harness_pending_input={"prompt": "do it", "key": "inbound-7"})

    await harness_turn_node(state, runtime(engine), config(6984))

    usage = loguru_records.events("llm_usage")
    assert len(usage) == 1
    extras = usage[0]["extra"]
    assert extras["source"] == "claude_code"
    assert extras["in_total"] == 1200
    assert extras["model"] == "claude-sonnet-5-5"
    assert extras["usage_kind"] == "agent"


# meiva:note
# Usage accounting is best effort: the turn has already finished when it is logged, so
# an unpriced bill must leave a hole in a row, never take the reply with it. The usage
# here carries more cache reads than prompt tokens, which is exactly what `quote`
# rejects — the shape a provider produces when the adapter misreports its fields.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def test_an_unpriceable_usage_keeps_the_reply_and_reports_the_gap(
    loguru_records: LogRecords,
) -> None:
    engine = FakeEngine(
        reply="done",
        usage=Usage(input_tokens=8, output_tokens=3, cache_read_input_tokens=99),
    )
    state = AgentState(harness_pending_input={"prompt": "do it", "key": "inbound-7"})

    command = await harness_turn_node(state, runtime(engine), config())

    assert command.update is not None
    assert command.update["harness_pending_input"] is None
    assert command.update["halted"] is True
    warned = loguru_records.events("harness_usage_log_failed")
    assert len(warned) == 1
    assert warned[0]["extra"]["agent_id"] == 6984
    assert loguru_records.events("llm_usage") == []


async def test_the_handoff_turn_lands_as_its_own_message(loguru_records: LogRecords) -> None:
    opening = HarnessTurn(
        turn_id="t0",
        text="acknowledged",
        usage=Usage(input_tokens=900, output_tokens=6),
        cost_usd=0.02,
        subtype="success",
        is_error=False,
        interrupted=False,
    )
    engine = FakeEngine(reply="done", prelude=(opening,))
    state = AgentState(harness_pending_input={"prompt": "do it", "key": "inbound-7"})

    command = await harness_turn_node(state, runtime(engine), config())

    assert command.update is not None
    assert command.update["harness_pending_input"] is None
    assert command.update["halted"] is True
    rows = loguru_records.events("llm_usage")
    assert [row["extra"]["in_total"] for row in rows] == [900, 7], "each turn keeps its own row"


async def test_a_faulted_session_settles_the_turn_as_a_fatal_stream_abort(
    loguru_records: LogRecords,
) -> None:
    class FaultingEngine(FakeEngine):
        async def run_turn(
            self,
            text: str,
            *,
            key: str,
            on_event: Callable[[BridgeEvent], None] | None = None,
        ) -> HarnessTurn:
            raise HarnessTurnFaultedError("cli died")

    state = AgentState(harness_pending_input={"prompt": "do it", "key": "inbound-7"})

    with pytest.raises(FatalLLMStreamError, match="claude_code harness turn aborted: cli died"):
        await harness_turn_node(state, runtime(FaultingEngine()), config())

    assert loguru_records.events("llm_usage") == [], "an aborted turn logs no usage"


# meiva:note
# A session that never started is not a crash: an agent whose provider CLI is missing
# or whose submission was refused settles the turn with a reported abort and idles for
# the next inbound, the same way a dead model stream does. Before this, the exception
# escaped the node and cost the host a crash per wake.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def test_a_session_that_cannot_start_settles_the_turn_as_a_fatal_stream_abort() -> None:
    class UnstartableEngine(FakeEngine):
        async def run_turn(
            self,
            text: str,
            *,
            key: str,
            on_event: Callable[[BridgeEvent], None] | None = None,
        ) -> HarnessTurn:
            raise HarnessSetupError("claude_code session could not start: no sdk")

    state = AgentState(harness_pending_input={"prompt": "do it", "key": "inbound-7"})

    with pytest.raises(FatalLLMStreamError, match="harness turn aborted"):
        await harness_turn_node(state, runtime(UnstartableEngine()), config())


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


async def test_replayed_completion_does_not_log_provider_usage_twice(
    loguru_records: LogRecords,
) -> None:
    engine = FakeEngine()
    turn = await engine.run_turn("work", key="inbound-1")
    ctx = runtime(engine).context
    log_turn_usage(ctx, 6984, engine.model, turn)
    log_turn_usage(ctx, 6984, engine.model, replace(turn, replayed=True))
    assert len(loguru_records.events("llm_usage")) == 1


# meiva:note
# `meiva.agents.get_last_message()` reads `agents_meta.last_message_text`, which only the
# native LLM node used to write — every harness agent therefore answered `None` for its
# whole life while its transcript held the text (#5511, measured on codex / claude /
# antigravity agents on 2026-10-21). The harness node now stamps it where the native one
# does, so the reader needs no branch of its own.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def test_a_completed_turn_stamps_the_reply_as_the_last_message(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
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
    engine = FakeEngine(reply="the answer")

    await harness_turn_node(
        AgentState(harness_pending_input={"prompt": "do it", "key": "inbound-7"}),
        runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources),
        config(agent_id),
    )

    assert _last_message_text(db_conn, agent_id) == "the answer"


async def test_the_handoff_prelude_is_not_the_last_message(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
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
    ack = HarnessTurn(
        turn_id="t0",
        text="acknowledged",
        usage=Usage(input_tokens=900, output_tokens=6),
        cost_usd=0.02,
        subtype="success",
        is_error=False,
        interrupted=False,
    )
    engine = FakeEngine(prelude=(ack,), reply="the answer")

    await harness_turn_node(
        AgentState(harness_pending_input={"prompt": "do it", "key": "inbound-7"}),
        runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources),
        config(agent_id),
    )

    assert _last_message_text(db_conn, agent_id) == "the answer", (
        "a prelude completes first; the reply lands last"
    )


async def test_intra_turn_events_project_to_live_sse_and_committed_messages() -> None:
    import json

    engine = FakeEngine(
        reply="all done",
        events=(
            TextDelta(turn_id="t1", text="Checking "),
            TextDelta(turn_id="t1", text="files..."),
            ToolUse(
                turn_id="t1",
                tool_use_id="call_1",
                name="Bash",
                tool_input={"command": "ls -la"},
            ),
            ToolResult(
                turn_id="t1",
                tool_use_id="call_1",
                content="README.md\n",
                is_error=False,
            ),
            TextDelta(turn_id="t1", text="all done"),
        ),
    )
    emitted: list[str] = []

    def record_emit(raw: str) -> None:
        emitted.append(str(json.loads(raw)["role"]))

    publisher = MagicMock()
    publisher.emit.side_effect = record_emit
    rt = runtime(engine, publisher=publisher)
    state = AgentState(harness_pending_input={"prompt": "list files", "key": "inbound-7"})

    command = await harness_turn_node(state, rt, config())

    assert command.update is not None
    assert command.update == {"halted": True, "harness_pending_input": None}

    assert emitted == [
        "chat_start",
        "chat_delta",
        "code_start",
        "code_delta",
        "timeline_snapshot",
        "exec_start",
        "exec_output",
        "chat_start",
        "chat_delta",
        "llm_done",
    ]


async def test_consecutive_assistant_text_blocks_project_to_distinct_ai_messages() -> None:
    import json

    engine = FakeEngine(
        reply="Second message",
        events=(
            TextDelta(turn_id="t1", text="First "),
            TextDelta(turn_id="t1", text="message"),
            AssistantText(turn_id="t1", text="First message"),
            TextDelta(turn_id="t1", text="Second "),
            TextDelta(turn_id="t1", text="message"),
            AssistantText(turn_id="t1", text="Second message"),
        ),
    )
    emitted: list[tuple[str, str | None]] = []

    def record_emit(raw: str) -> None:
        payload = json.loads(raw)
        emitted.append((str(payload["role"]), payload.get("item_id")))

    publisher = MagicMock()
    publisher.emit.side_effect = record_emit
    rt = runtime(engine, publisher=publisher)
    state = AgentState(harness_pending_input={"prompt": "two steps", "key": "inbound-7"})

    command = await harness_turn_node(state, rt, config(6985))

    assert command.update is not None
    assert command.update == {"halted": True, "harness_pending_input": None}
    assert emitted == [
        ("chat_start", "0.0"),
        ("chat_delta", "0.0"),
        ("chat_start", "1.0"),
        ("chat_delta", "1.0"),
        ("llm_done", None),
    ]


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


async def test_harness_turn_projects_reasoning_and_step_timing() -> None:
    import json

    engine = FakeEngine(
        events=(
            ReasoningDelta(turn_id="t1", text="Planning "),
            ReasoningDelta(turn_id="t1", text="tool call"),
            ReasoningText(turn_id="t1", text="Planning tool call"),
            ToolUse(
                turn_id="t1",
                tool_use_id="call-1",
                name="Bash",
                tool_input={"command": "echo ok"},
            ),
            ToolResult(
                turn_id="t1",
                tool_use_id="call-1",
                is_error=False,
                content="ok\n",
            ),
            ReasoningText(turn_id="t1", text="Synthesizing reply"),
            AssistantText(turn_id="t1", text="Final answer"),
        ),
    )
    emitted: list[tuple[str, str | None]] = []

    def record_emit(raw: str) -> None:
        payload = json.loads(raw)
        emitted.append((str(payload["role"]), payload.get("item_id")))

    publisher = MagicMock()
    publisher.emit.side_effect = record_emit
    rt = runtime(engine, publisher=publisher)
    state = AgentState(harness_pending_input={"prompt": "run it", "key": "inbound-9"})

    command = await harness_turn_node(state, rt, config(6985))
    assert command.update is not None
    assert command.update == {"halted": True, "harness_pending_input": None}
    assert ("reasoning_start", "0.0") in emitted
    assert ("reasoning_delta", "0.0") in emitted
    assert ("code_start", "0.1") in emitted
    assert ("reasoning_start", "2.0") in emitted
    assert ("chat_start", "2.1") in emitted


async def test_terminate_interrupt_preserves_projected_tool_call_messages() -> None:
    from agent.graph.interrupt import ModelInterruptedError
    from base.agents.messages.inbound import InterruptReason

    class SelfTerminatingEngine(FakeEngine):
        async def run_turn(
            self,
            text: str,
            *,
            key: str,
            on_event: Callable[[BridgeEvent], None] | None = None,
        ) -> HarnessTurn:
            self.submitted.append((text, key))
            assert on_event is not None
            on_event(ReasoningText(turn_id="t1", text="Role complete, terminating."))
            on_event(
                ToolUse(
                    turn_id="t1",
                    tool_use_id="call-term",
                    name="Bash",
                    tool_input={"command": "python3 -c 'import meiva; meiva.self.terminate()'"},
                )
            )
            raise ModelInterruptedError(InterruptReason.SYSTEM)

    engine = SelfTerminatingEngine()
    state = AgentState(harness_pending_input={"prompt": "[Heartbeat]: check in", "key": "window-4"})

    command = await harness_turn_node(state, runtime(engine), config(6986))
    assert engine.submitted == [("[Heartbeat]: check in", "window-4")]
    assert command.update is not None
    assert command.update == {"halted": True, "harness_pending_input": None}
