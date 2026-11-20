import json
import random
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol

import pytest
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel, Field

from agent.execution.protocol import ResultPayload
from agent.graph.exec.node import ExecDone, ExecGoto, exec_node_impl
from agent.graph.exec.process import ExecProcess, SubprocessExecProcess
from agent.graph.exec.tests.process_inputs import (
    ReportingExecProcess,
    ScriptedExecProcess,
    live_parent_facts,
)
from agent.graph.tool_calls import normalize_tool_calls
from agent.state import AgentState, build_agent_state
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import read_meiva_kwargs
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.events.live.tests.live_capture import LiveEvents, idle_publisher
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_mapping
from base.host.env.agent_slices import AgentSlices
from base.log.tests.log_records import LogRecords
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.paths import meiva_home, resolve_meiva_home
from meiva.tests.fixtures.pin_agent import snapshot_clients


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


# meiva:note
# The slice of langgraph's builder and compiled graph these tests use. Its stubs leave `add_node`,
# `compile` and `ainvoke` partly unknown, so a graph is handed through these protocols once and
# read only through the signatures declared here.
class _RuntimeNode(Protocol):
    def __call__(
        self, state: AgentState, *, runtime: Runtime[MeivaContext]
    ) -> Awaitable[object]: ...


class _Compiled(Protocol):
    def ainvoke(
        self, input: AgentState | None, config: RunnableConfig, *, context: MeivaContext
    ) -> Awaitable[object]: ...

    def aget_state(self, config: RunnableConfig) -> Awaitable[StateSnapshot]: ...


class _Builder(Protocol):
    def add_node(
        self, node: str, action: _RuntimeNode, *, input_schema: type[AgentState]
    ) -> object: ...

    def add_edge(self, start_key: str, end_key: str) -> object: ...

    def compile(
        self, checkpointer: BaseCheckpointSaver[str] | None, *, interrupt_after: list[str] | None
    ) -> _Compiled: ...


def _wire(
    builder: _Builder,
    state_cls: type[AgentState],
    process: ExecProcess,
    checkpointer: BaseCheckpointSaver[str] | None,
    interrupt_after: list[str] | None,
) -> _Compiled:
    async def execute(state: AgentState, *, runtime: Runtime[MeivaContext]) -> Command[ExecGoto]:
        return await exec_node_impl(state, runtime, get_config(), process=process)

    async def claim(state: AgentState, *, runtime: Runtime[MeivaContext]) -> dict[str, object]:
        return {}

    _ = builder.add_node("exec", execute, input_schema=state_cls)
    _ = builder.add_node("claim", claim, input_schema=state_cls)
    _ = builder.add_edge(START, "exec")
    _ = builder.add_edge("claim", END)
    return builder.compile(checkpointer, interrupt_after=interrupt_after)


def _graph(
    state_cls: type[AgentState],
    *,
    process: ExecProcess,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    interrupt_after: list[str] | None = None,
) -> _Compiled:
    builder = StateGraph(state_cls, context_schema=MeivaContext)
    return _wire(builder, state_cls, process, checkpointer, interrupt_after)


# meiva:note
# What a graph run left in the state: its channels by name, with the message list read as
# messages. The first message is the assistant turn the calls came from, the rest are the results
# and notes the exec node committed.
@dataclass(frozen=True)
class _Run:
    values: Mapping[str, object]

    @property
    def messages(self) -> list[BaseMessage]:
        return _messages_of(self.values)

    def results(self, stop: int | None = None) -> list[ToolMessage]:
        return _tool_results(self.messages[1:stop])


def _messages_of(values: Mapping[str, object]) -> list[BaseMessage]:
    messages = values["messages"]
    assert is_object_list(messages)
    assert all(isinstance(message, BaseMessage) for message in messages)
    return [message for message in messages if isinstance(message, BaseMessage)]


def _tool_results(messages: list[BaseMessage]) -> list[ToolMessage]:
    results = [message for message in messages if isinstance(message, ToolMessage)]
    assert len(results) == len(messages)
    return results


def _run_of(raw: object) -> _Run:
    assert is_object_dict(raw)
    return _Run(raw)


async def _run_calls(
    state: AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
    *,
    process: ExecProcess,
) -> _Run:
    graph = _graph(type(state), process=process)
    raw: object = await graph.ainvoke(state, context=runtime.context, config=config)
    return _run_of(raw)


async def test_calls_execute_separately_without_rewriting_assistant() -> None:
    ai = AIMessage(
        id="ai-multi",
        content="Two independent calls.",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "first()"}, "id": "call-a"},
            {"name": "execute_code", "args": {"code": "second()"}, "id": "call-b"},
        ],
    )
    original = ai.model_dump()
    process = ReportingExecProcess(
        [
            (ExecDone(output=""), ResultPayload(kind="done", state_update={}, sdk_calls=[])),
            (ExecDone(output=""), ResultPayload(kind="done", state_update={}, sdk_calls=[])),
        ]
    )

    command = await _run_calls(
        AgentState(messages=[ai]),
        _node_runtime(),
        {"configurable": {"thread_id": "7"}},
        process=process,
    )

    assert ai.model_dump() == original
    messages = command.results()
    assert [message.tool_call_id for message in messages] == ["call-a", "call-b"]
    assert "first()" in messages[0].text
    assert "second()" in messages[1].text


class _MessagesPlugin(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)


def _messages_state_class() -> type[AgentState]:
    return build_agent_state(
        ExtensionRegistry((("exec_notes", PluginContributions(state=(_MessagesPlugin,))),))
    )


def _messages_state(*codes: str) -> AgentState:
    return _messages_state_class()(messages=_state(*codes).messages)


def _state(*codes: str) -> AgentState:
    return AgentState(
        messages=[
            AIMessage(
                id="ai-batch",
                content="",
                tool_calls=[
                    {"name": "execute_code", "args": {"code": code}, "id": f"call-{i}"}
                    for i, code in enumerate(codes)
                ],
            )
        ]
    )


def _runtime(
    database_gate: ProcessDbGate, agent: AgentSlices | None = None
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            meiva_home=resolve_meiva_home(),
            event_publisher=idle_publisher(),
            agent=agent or AgentSlices.resolve(default_reader=defaults()),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            clients=snapshot_clients(),
            identity=AgentIdentity(agent_id=7, owns_loop=True),
            catalog=build_model_catalog(home=meiva_home()),
            clock_factory=Clock.from_settings,
        )
    )


def _node_runtime(
    agent: AgentSlices | None = None, publisher: AgentEventPublisher | None = None
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            meiva_home=resolve_meiva_home(),
            event_publisher=publisher or idle_publisher(),
            agent=agent or AgentSlices.resolve(default_reader=defaults()),
            identity=AgentIdentity(agent_id=7, owns_loop=True),
            catalog=build_model_catalog(home=meiva_home()),
            clock_factory=Clock.from_settings,
        )
    )


async def test_real_children_do_not_share_globals(database_gate: ProcessDbGate) -> None:
    result = await _run_calls(
        _state('shared_name = 42; print("first")', 'print("shared_name" in globals())'),
        _runtime(database_gate),
        {"configurable": {"thread_id": "7"}},
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )
    first, second = result.results()
    assert first.tool_call_id == "call-0"
    assert second.tool_call_id == "call-1"
    assert "first" in first.text
    assert "False" in second.text


async def test_exception_does_not_skip_later_call(database_gate: ProcessDbGate) -> None:
    result = await _run_calls(
        _state('raise ValueError("first failed")', 'print("second ran")'),
        _runtime(database_gate),
        {"configurable": {"thread_id": "7"}},
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )
    first, second = result.results()
    assert "ValueError: first failed" in first.text
    assert "second ran" in second.text


async def test_plugin_reducers_commit_between_calls() -> None:
    from operator import add
    from typing import Annotated

    class CounterState(AgentState):
        total: Annotated[int, add] = 10

    state = CounterState(messages=_state("first()", "second()").messages)

    process = ReportingExecProcess(
        [
            (ExecDone(output=""), ResultPayload(kind="done", state_update={"total": 1})),
            (ExecDone(output=""), ResultPayload(kind="done", state_update={"total": 1})),
        ],
        report=("total",),
    )

    result = await _run_calls(
        state, _node_runtime(), {"configurable": {"thread_id": "7"}}, process=process
    )
    assert state.total == 10
    first, second = result.results()
    assert "total=10" in first.text
    assert "total=11" in second.text
    assert result.values["total"] == 12


async def test_notes_and_media_follow_all_results_and_stream_ids_match(
    tmp_path: Path, live_events: LiveEvents
) -> None:
    note = HumanMessage(content="context note")
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG\r\n\x1a\n")
    process = ScriptedExecProcess(
        [
            (
                ExecDone(output="first"),
                ResultPayload(
                    kind="done",
                    state_update={
                        "messages": [note],
                        "attach": [{"path": str(chart), "label": "the chart"}],
                    },
                    sdk_calls=[],
                ),
            ),
            (ExecDone(output="second"), ResultPayload(kind="done", state_update={}, sdk_calls=[])),
        ]
    )

    result = await _run_calls(
        _messages_state("first()", "second()"),
        _node_runtime(
            AgentSlices.resolve(pins={"llm_model": "claude-sonnet-5"}, default_reader=defaults()),
            live_events.publisher,
        ),
        {"configurable": {"thread_id": "7"}},
        process=process,
    )

    messages = result.messages[1:]
    assert [message.tool_call_id for message in _tool_results(messages[:2])] == [
        "call-0",
        "call-1",
    ]
    assert messages[2] == note
    media = messages[3]
    assert isinstance(media, HumanMessage) and len(messages) == 4
    assert read_meiva_kwargs(media).get("meiva_msg_type") == "attach"
    assert "the chart" in json.dumps(media.content)
    events = await live_events.events()
    assert [event["item_id"] for event in events] == ["1.0", "1.0", "2.0", "2.0"]


@pytest.mark.parametrize("outcome", ["cancel", "restart", "terminate", "compact", "timeout"])
async def test_lifecycle_pairs_skipped_calls_and_timeout_continues(outcome: str) -> None:
    from agent.graph.exec._result import ExecLifecycleKind
    from agent.graph.exec.node import ExecCancelled, ExecLifecycle, ExecTimedOut

    outcomes = {
        "cancel": ExecCancelled(output="cancelled"),
        "restart": ExecLifecycle(output="restart", kind=ExecLifecycleKind.RESTART),
        "terminate": ExecLifecycle(output="terminate", kind=ExecLifecycleKind.TERMINATION),
        "compact": ExecLifecycle(output="compact", kind=ExecLifecycleKind.SYSTEM_HALT),
        "timeout": ExecTimedOut(output="timeout"),
    }
    process = ScriptedExecProcess(
        [
            (outcomes[outcome], None),
            (ExecDone(output="second"), ResultPayload(kind="done", state_update={}, sdk_calls=[])),
        ]
    )

    result = await _run_calls(
        _state("first()", "second()"),
        _node_runtime(),
        {"configurable": {"thread_id": "7"}},
        process=process,
    )
    if outcome == "compact":
        assert len(result.messages) == 1
    else:
        first, second = result.results()
        assert [first.tool_call_id, second.tool_call_id] == ["call-0", "call-1"]
        assert ("second" if outcome == "timeout" else "Not executed") in second.text
    assert result.values["halted"] is (outcome != "timeout")


# meiva:note
# The turn-end declaration is agent input, so it is validated where it enters the state and
# never reaches a `turn_end` record outside the closed set.
# meiva:link agent/graph/llm/docs/turn-records.md
async def test_a_turn_end_declaration_reaches_the_state(database_gate: ProcessDbGate) -> None:
    result = await _run_calls(
        _state(
            "import meiva\n"
            "meiva.state_update['ended_with'] = 'deferred:time'\n"
            "meiva.state_update['ended_with_reason'] = 'night, continue after 09:00'\n"
        ),
        _runtime(database_gate),
        _config(),
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )

    assert result.values["ended_with"] == "deferred:time"
    assert result.values["ended_with_reason"] == "night, continue after 09:00"


# meiva:note
# A refused declaration costs the turn nothing — that is the approved exception to fail-fast
# for this field (`base/agents/observation/ended_with.py`): the record says `invalid` (made
# and refused, not absent) and the text the agent wrote rides the rejection event.
async def test_a_turn_end_declaration_outside_the_set_is_recorded_invalid(
    loguru_records: LogRecords, database_gate: ProcessDbGate
) -> None:
    result = await _run_calls(
        _state("import meiva\nmeiva.state_update['ended_with'] = 'finished for today'\n"),
        _runtime(database_gate),
        _config(),
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )

    assert result.values["ended_with"] == "invalid"
    assert [m.type for m in result.messages] == ["ai", "tool"], "the turn must run to the end"
    (record,) = loguru_records.extras(event="turn_end_declaration_rejected")
    assert record["raw_ended_with"] == "finished for today"
    assert record["problem"] is not None


# meiva:note
# `waiting:` with no object is the same path as an out-of-set value: refused, not fatal.
async def test_a_waiting_declaration_without_its_object_is_recorded_invalid(
    loguru_records: LogRecords, database_gate: ProcessDbGate
) -> None:
    result = await _run_calls(
        _state("import meiva\nmeiva.state_update['ended_with'] = 'waiting:'\n"),
        _runtime(database_gate),
        _config(),
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )

    assert result.values["ended_with"] == "invalid"
    (record,) = loguru_records.extras(event="turn_end_declaration_rejected")
    assert record["raw_ended_with"] == "waiting:"
    assert "names no object" in str(record["problem"])


async def test_unknown_tool_does_not_consume_sibling_code() -> None:
    state = _state("print('must not run')", "print('runs')")
    ai = state.messages[-1]
    assert isinstance(ai, AIMessage)
    ai.tool_calls[0]["name"] = "meiva.files.edit"
    process = ReportingExecProcess(
        [(ExecDone(output=""), ResultPayload(kind="done", state_update={}, sdk_calls=[]))]
    )

    result = await _run_calls(
        state, _node_runtime(), {"configurable": {"thread_id": "7"}}, process=process
    )
    first, second = result.results()
    assert "unknown tool" in first.text
    assert "print('runs')" in second.text
    assert "must not run" not in first.text + second.text


def _config() -> RunnableConfig:
    return {"configurable": {"thread_id": "7"}}


def _ai_with_two_content_tool_uses() -> AIMessage:
    first_code = 'print("skills")'
    second_code = 'print("agents")'
    return AIMessage(
        id="ai_multi",
        content=[
            {"type": "thinking", "thinking": "plan", "index": 0},
            {
                "type": "tool_use",
                "id": "call_00",
                "name": "execute_code",
                "input": {},
                "partial_json": json.dumps({"code": first_code}),
                "index": 1,
            },
            {
                "type": "tool_use",
                "id": "call_01",
                "name": "execute_code",
                "input": {},
                "partial_json": json.dumps({"code": second_code}),
                "index": 2,
            },
        ],
        tool_calls=[
            {"name": "execute_code", "args": {"code": first_code}, "id": "call_00"},
        ],
        response_metadata={"stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


def test_normalize_recovers_each_content_call_without_merging() -> None:
    original = _ai_with_two_content_tool_uses()
    normalized = normalize_tool_calls(original)
    assert normalized is not None
    assert normalized.id == original.id
    assert normalized.content == original.content
    assert normalized.response_metadata == original.response_metadata
    assert normalized.usage_metadata == original.usage_metadata
    assert [call["id"] for call in normalized.tool_calls] == ["call_00", "call_01"]
    assert [call["args"]["code"] for call in normalized.tool_calls] == [
        'print("skills")',
        'print("agents")',
    ]
    assert normalize_tool_calls(normalized) is None


async def test_exec_recovers_missing_content_call_without_syntax_plugin(
    database_gate: ProcessDbGate,
) -> None:
    state = AgentState(messages=[_ai_with_two_content_tool_uses()])
    result = await _run_calls(
        state,
        _runtime(database_gate),
        _config(),
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )
    fixed_ai = result.messages[0]
    assert isinstance(fixed_ai, AIMessage)
    first, second = result.results()
    assert len(fixed_ai.tool_calls) == 2
    assert first.tool_call_id == "call_00"
    assert second.tool_call_id == "call_01"
    assert "skills" in first.text
    assert "agents" in second.text


def _ai_with_hallucinated_tool_name() -> AIMessage:
    return AIMessage(
        id="ai_hallucinated",
        content=[],
        tool_calls=[
            {
                "name": "meiva.files.edit",
                "args": {"path": "/testbed/x.py", "old": "a", "new": "b"},
                "id": "call_99",
            },
        ],
        response_metadata={"stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )


async def test_exec_node_feeds_back_on_unknown_tool_name(database_gate: ProcessDbGate) -> None:
    state = AgentState(messages=[_ai_with_hallucinated_tool_name()])

    result = await _run_calls(
        state,
        _runtime(database_gate),
        _config(),
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )

    assert result.values["halted"] is False
    (tool_msg,) = result.results()
    assert tool_msg.tool_call_id == "call_99"
    assert "meiva.files.edit" in tool_msg.text
    assert "execute_code" in tool_msg.text


async def test_exec_node_feeds_back_when_code_key_missing(database_gate: ProcessDbGate) -> None:
    ai = AIMessage(
        id="ai_no_code",
        content=[],
        tool_calls=[{"name": "execute_code", "args": {"foo": "bar"}, "id": "call_88"}],
        response_metadata={"stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    state = AgentState(messages=[ai])

    result = await _run_calls(
        state,
        _runtime(database_gate),
        _config(),
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )

    (tool_msg,) = result.results()
    assert tool_msg.tool_call_id == "call_88"
    assert "execute_code" in tool_msg.text


async def test_langgraph_owns_each_call_state_transition() -> None:
    from typing import Annotated

    def append_digit(current: int, digit: int) -> int:
        return current * 10 + digit

    class DecimalState(AgentState):
        total: Annotated[int, append_digit] = 0

    process = ReportingExecProcess(
        [
            (ExecDone(output=""), ResultPayload(kind="done", state_update={"total": 1})),
            (ExecDone(output=""), ResultPayload(kind="done", state_update={"total": 2})),
        ],
        report=("total",),
    )

    state = DecimalState(total=7, messages=_state("first()", "second()").messages)
    result = await _run_calls(state, _node_runtime(), _config(), process=process)
    first, second = result.results()
    assert "total=7" in first.text
    assert "total=71" in second.text
    assert result.values["total"] == 712


async def test_checkpoint_resume_keeps_results_and_deferred_notes() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    note = HumanMessage(content="context from first call", id="note-first")
    process = ReportingExecProcess(
        [
            (
                ExecDone(output=""),
                ResultPayload(kind="done", state_update={"messages": [note]}, sdk_calls=None),
            ),
            (ExecDone(output=""), ResultPayload(kind="done", state_update={}, sdk_calls=[])),
        ],
        report=("pending_exec_notes",),
    )

    graph = _graph(
        _messages_state_class(),
        checkpointer=InMemorySaver(),
        interrupt_after=["exec"],
        process=process,
    )
    config = _config()
    runtime = _node_runtime()
    _ = await graph.ainvoke(_state("first()", "second()"), config, context=runtime.context)
    checkpoint = await graph.aget_state(config)
    assert checkpoint.next == ("exec",)
    saved = _Run(object_mapping(checkpoint.values))
    assert len(saved.messages) == 2
    last = saved.messages[-1]
    assert isinstance(last, ToolMessage) and last.tool_call_id == "call-0"
    assert saved.values["pending_exec_notes"] == [note]

    result = _run_of(await graph.ainvoke(None, config, context=runtime.context))
    first, second = result.results(3)
    assert [first.tool_call_id, second.tool_call_id] == ["call-0", "call-1"]
    assert result.messages[3:] == [note]
    assert result.values["pending_exec_notes"] == []
    assert "pending_exec_notes=[]" in first.text
    assert "context from first call" in second.text
    assert "sdk_calls" not in read_meiva_kwargs(first)
    assert read_meiva_kwargs(second).get("sdk_calls") == []


async def test_write_uids_survive_child_replay_and_distinguish_writes(
    database_gate: ProcessDbGate,
) -> None:
    code = (
        "import meiva\n"
        "print('uids', meiva.context.sdk_operation_uid(), meiva.context.sdk_operation_uid())"
    )
    state = _state(code, code)
    runtime = _runtime(database_gate)
    process = SubprocessExecProcess(live_parent_facts(), rng=_rng())

    first = await _run_calls(state, runtime, _config(), process=process)
    replay = await _run_calls(state, runtime, _config(), process=process)
    writes = [
        line.removeprefix("uids ").split()
        for message in first.results()
        for line in message.text.splitlines()
        if line.startswith("uids ")
    ]
    replayed = [
        line.removeprefix("uids ").split()
        for message in replay.results()
        for line in message.text.splitlines()
        if line.startswith("uids ")
    ]
    assert [len(call) for call in writes] == [2, 2]
    assert len({uid for call in writes for uid in call}) == 4
    assert replayed == writes
