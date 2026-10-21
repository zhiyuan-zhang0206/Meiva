from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.claim.harness_turn import (
    harness_turn_node,
    interrupt_engine,
    log_turn_usage,
    pending_messages,
    render_messages,
    turn_key,
)
from agent.graph.llm_errors import FatalLLMStreamError
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import Harness
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import HarnessSetupError, HarnessTurn, HarnessTurnFaultedError, Usage
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.packages.plugin_providers import build_model_catalog


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
    ) -> None:
        self.harness = Harness.CLAUDE_CODE
        self.model = "claude-sonnet-5-5"
        self.submitted: list[tuple[str, str]] = []
        self.interrupts = 0
        self.closed = False
        self._reply = reply
        self._usage = usage
        self._prelude = prelude

    async def run_turn(self, text: str, *, key: str) -> HarnessTurn:
        self.submitted.append((text, key))
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


def runtime(engine: FakeEngine | None) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            harness=Harness.CLAUDE_CODE if engine is not None else Harness.MEIVA,
            harness_bridge=engine,
            llm=MagicMock(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(default_reader=defaults()),
            catalog=build_model_catalog(),
            clock_factory=Clock.from_settings,
        )
    )


def inbound(text: str, inbound_id: int) -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs={"meiva_msg_type": "inbound", "meiva_inbound_id": inbound_id},
    )


def config(agent_id: int = 6984) -> RunnableConfig:
    return {"configurable": {"thread_id": str(agent_id)}}


def test_the_standing_head_is_not_pending() -> None:
    state = AgentState(
        messages=[SystemMessage(content="prompt"), HumanMessage(content="agent id note")],
        harness_cursor=2,
    )
    state.messages.append(inbound("hello", 11))

    assert [message.content for message in pending_messages(state)] == ["hello"]


def test_a_shrunk_window_submits_nothing_rather_than_resending() -> None:
    state = AgentState(messages=[inbound("hello", 11)], harness_cursor=9)

    assert pending_messages(state) == []


def test_own_replies_are_never_pending() -> None:
    state = AgentState(messages=[inbound("hello", 11), AIMessage(content="hi")], harness_cursor=0)

    assert [message.content for message in pending_messages(state)] == ["hello"]


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


async def test_a_turn_submits_the_pending_messages_and_commits_the_reply() -> None:
    engine = FakeEngine(reply="done")
    state = AgentState(
        messages=[SystemMessage(content="prompt"), inbound("do it", 7)], harness_cursor=1
    )

    command = await harness_turn_node(state, runtime(engine), config())

    assert command.goto == "claim"
    assert command.update is not None
    assert engine.submitted == [("do it", "inbound-7")]
    reply = command.update["messages"][0]
    assert isinstance(reply, AIMessage) and reply.content == "done"
    assert command.update["harness_cursor"] == 3, "the reply is covered by the cursor too"
    assert command.update["halted"] is True


async def test_a_turn_with_nothing_pending_ends_without_calling_the_engine() -> None:
    engine = FakeEngine()
    state = AgentState(messages=[SystemMessage(content="prompt")], harness_cursor=1)

    command = await harness_turn_node(state, runtime(engine), config())

    assert command.goto == "claim"
    assert engine.submitted == []
    assert command.update == {"halted": True, "harness_cursor": 1}


async def test_a_turn_logs_usage_under_the_harness(loguru_records: LogRecords) -> None:
    engine = FakeEngine(usage=Usage(input_tokens=1200, output_tokens=34))
    state = AgentState(messages=[inbound("do it", 7)], harness_cursor=0)

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
    state = AgentState(messages=[inbound("do it", 7)], harness_cursor=0)

    command = await harness_turn_node(state, runtime(engine), config())

    assert command.update is not None
    assert [message.content for message in command.update["messages"]] == ["done"]
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
    state = AgentState(messages=[inbound("do it", 7)], harness_cursor=0)

    command = await harness_turn_node(state, runtime(engine), config())

    assert command.update is not None
    assert [message.content for message in command.update["messages"]] == ["acknowledged", "done"]
    assert command.update["harness_cursor"] == 3, "both messages are covered by the cursor"
    rows = loguru_records.events("llm_usage")
    assert [row["extra"]["in_total"] for row in rows] == [900, 7], "each turn keeps its own row"


async def test_a_faulted_session_settles_the_turn_as_a_fatal_stream_abort(
    loguru_records: LogRecords,
) -> None:
    class FaultingEngine(FakeEngine):
        async def run_turn(self, text: str, *, key: str) -> HarnessTurn:
            raise HarnessTurnFaultedError("cli died")

    state = AgentState(messages=[inbound("do it", 7)], harness_cursor=0)

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
        async def run_turn(self, text: str, *, key: str) -> HarnessTurn:
            raise HarnessSetupError("claude_code session could not start: no sdk")

    state = AgentState(messages=[inbound("do it", 7)], harness_cursor=0)

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
