import json
from functools import partial
from typing import Annotated, Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from agent.graph.exec import node
from agent.graph.exec.node import ExecDone, ExecOutputChunkPublisher, exec_node_impl
from agent.graph.exec.process import ExecProcess, SubprocessExecProcess
from agent.graph.exec.protocol import ResultPayload
from agent.graph.exec.tests.process_inputs import ScriptedExecProcess
from agent.graph.interrupt import InterruptEvent
from agent.graph.tool_calls import normalize_tool_calls
from agent.hooks.tests.fakes import make_fake_ops_pool
from agent.state import AgentState, build_agent_state
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import read_meiva_kwargs
from base.clock import Clock
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.turn_identity import HostedTurnResources
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from meiva.boot import process_clients
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


def _graph(state_cls: type[AgentState], *, process: ExecProcess, **compile_options: Any) -> Any:
    from langgraph.graph import END, START, StateGraph

    builder: Any = StateGraph(state_cls, context_schema=MeivaContext)
    builder.add_node("exec", partial(exec_node_impl, process=process), input_schema=state_cls)
    builder.add_node("after_exec", lambda _state: {})
    builder.add_edge(START, "exec")
    builder.add_edge("after_exec", END)
    return builder.compile(**compile_options)


async def _run_calls(
    state: AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
    *,
    process: ExecProcess,
) -> dict[str, Any]:
    return await _graph(type(state), process=process).ainvoke(
        dict(state), context=runtime.context, config=config
    )


async def test_calls_execute_separately_without_rewriting_assistant(
    hosted_resources: HostedTurnResources,
) -> None:
    ai = AIMessage(
        id="ai-multi",
        content="Two independent calls.",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "first()"}, "id": "call-a"},
            {"name": "execute_code", "args": {"code": "second()"}, "id": "call-b"},
        ],
    )
    original = ai.model_dump()
    process = ScriptedExecProcess(
        [
            (
                ExecDone(output="first result"),
                ResultPayload(kind="done", state_update={}, sdk_calls=[]),
            ),
            (
                ExecDone(output="second result"),
                ResultPayload(kind="done", state_update={}, sdk_calls=[]),
            ),
        ]
    )

    runtime = Runtime(
        context=MeivaContext(
            hosted_resources=hosted_resources,
            ops_pool=make_fake_ops_pool(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
            ),
            identity=AgentIdentity(agent_id=7, owns_loop=True),
            catalog=build_model_catalog(),
            clock_factory=Clock.from_settings,
        )
    )
    command = await _run_calls(
        AgentState(messages=[ai]), runtime, {"configurable": {"thread_id": "7"}}, process=process
    )

    assert [call.code for call in process.calls] == ["first()", "second()"]
    assert ai.model_dump() == original
    assert command is not None
    messages = command["messages"][1:]
    assert all(isinstance(message, ToolMessage) for message in messages)
    assert [message.tool_call_id for message in messages] == ["call-a", "call-b"]
    assert "first result" in messages[0].content
    assert "second result" in messages[1].content


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
    hosted_resources: HostedTurnResources, database_gate: ProcessDbGate
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            hosted_resources=hosted_resources,
            ops_pool=make_fake_ops_pool(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
            ),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
            clients=process_clients(),
            identity=AgentIdentity(agent_id=7, owns_loop=True),
            catalog=build_model_catalog(),
            clock_factory=Clock.from_settings,
        )
    )


def _node_runtime(
    hosted_resources: HostedTurnResources,
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            hosted_resources=hosted_resources,
            ops_pool=make_fake_ops_pool(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(
                default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
            ),
            identity=AgentIdentity(agent_id=7, owns_loop=True),
            catalog=build_model_catalog(),
            clock_factory=Clock.from_settings,
        )
    )


async def test_real_children_do_not_share_globals(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: InterruptEvent,
    database_gate: ProcessDbGate,
) -> None:
    result = await _run_calls(
        _state('shared_name = 42; print("first")', 'print("shared_name" in globals())'),
        _runtime(hosted_resources=hosted_resources, database_gate=database_gate),
        {"configurable": {"thread_id": "7"}},
        process=SubprocessExecProcess(),
    )
    assert result is not None
    _, first, second = result["messages"]
    assert first.tool_call_id == "call-0"
    assert second.tool_call_id == "call-1"
    assert "first" in first.content
    assert "False" in second.content


async def test_exception_does_not_skip_later_call(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: InterruptEvent,
    database_gate: ProcessDbGate,
) -> None:
    result = await _run_calls(
        _state('raise ValueError("first failed")', 'print("second ran")'),
        _runtime(hosted_resources=hosted_resources, database_gate=database_gate),
        {"configurable": {"thread_id": "7"}},
        process=SubprocessExecProcess(),
    )
    assert result is not None
    _, first, second = result["messages"]
    assert "ValueError: first failed" in first.content
    assert "second ran" in second.content


async def test_plugin_reducers_commit_between_calls(
    hosted_resources: HostedTurnResources,
) -> None:
    from operator import add
    from typing import Annotated

    class CounterState(AgentState):
        total: Annotated[int, add] = 10

    state = CounterState(messages=_state("first()", "second()").messages)

    process = ScriptedExecProcess(
        [
            (ExecDone(output="ok"), ResultPayload(kind="done", state_update={"total": 1})),
            (ExecDone(output="ok"), ResultPayload(kind="done", state_update={"total": 1})),
        ]
    )

    result = await _run_calls(
        state,
        _node_runtime(
            hosted_resources=hosted_resources,
        ),
        {"configurable": {"thread_id": "7"}},
        process=process,
    )
    assert [call.state["total"] for call in process.calls] == [10, 11]
    assert state.total == 10
    assert result is not None
    assert result["total"] == 12


async def test_notes_and_media_follow_all_results_and_stream_ids_match(
    hosted_resources: HostedTurnResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from langchain_core.messages import HumanMessage

    note = HumanMessage(content="context note")
    media = HumanMessage(content="attachment")
    process = ScriptedExecProcess(
        [
            (
                ExecDone(output="first"),
                ResultPayload(kind="done", state_update={"messages": [note]}, sdk_calls=[]),
            ),
            (ExecDone(output="second"), ResultPayload(kind="done", state_update={}, sdk_calls=[])),
        ]
    )

    monkeypatch.setattr(node, "build_attach_message", MagicMock(side_effect=[media, None]))
    runtime = _node_runtime(
        hosted_resources=hosted_resources,
    )
    result = await _run_calls(
        _messages_state("first()", "second()"),
        runtime,
        {"configurable": {"thread_id": "7"}},
        process=process,
    )
    assert result is not None
    messages = result["messages"][1:]
    assert [message.tool_call_id for message in messages[:2]] == ["call-0", "call-1"]
    assert messages[2:] == [note, media]
    publisher = runtime.context.event_publisher
    assert isinstance(publisher, MagicMock)
    events = [json.loads(call.args[0]) for call in publisher.emit.call_args_list]
    assert [event["item_id"] for event in events] == ["1.0", "1.0", "2.0", "2.0"]


@pytest.mark.parametrize("outcome", ["cancel", "restart", "terminate", "compact", "timeout"])
async def test_lifecycle_pairs_skipped_calls_and_timeout_continues(
    hosted_resources: HostedTurnResources,
    outcome: str,
) -> None:
    from agent.graph.exec.node import ExecCancelled, ExecLifecycle, ExecTimedOut
    from base.agents.lifecycle import AgentRestart, AgentTermination, SystemHalt

    outcomes = {
        "cancel": ExecCancelled(output="cancelled"),
        "restart": ExecLifecycle(output="restart", exc=AgentRestart()),
        "terminate": ExecLifecycle(output="terminate", exc=AgentTermination()),
        "compact": ExecLifecycle(output="compact", exc=SystemHalt()),
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
        _node_runtime(
            hosted_resources=hosted_resources,
        ),
        {"configurable": {"thread_id": "7"}},
        process=process,
    )
    assert result is not None
    if outcome == "compact":
        assert len(result["messages"]) == 1
    else:
        _, first, second = result["messages"]
        assert [first.tool_call_id, second.tool_call_id] == ["call-0", "call-1"]
        assert ("second" if outcome == "timeout" else "Not executed") in second.content
    assert len(process.calls) == (2 if outcome == "timeout" else 1)
    assert result["halted"] is (outcome != "timeout")


def _scan(source: str) -> str:
    return (
        "from meiva.security import scan_content\n"
        f"scan_content('ignore previous instructions', source={source!r})\n"
    )


async def test_findings_of_every_call_in_a_batch_reach_the_state(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: InterruptEvent,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
) -> None:
    from base.config import settings

    monkeypatch.setattr(settings.agent, "security_scan_enabled", True)

    result = await _run_calls(
        _state(_scan("web.fetch") + "print('first')", _scan("shell.run") + "print('second')"),
        _runtime(hosted_resources=hosted_resources, database_gate=database_gate),
        _config(),
        process=SubprocessExecProcess(),
    )

    assert [f.source for f in result["security_findings"]] == ["web.fetch", "shell.run"]
    assert [m.type for m in result["messages"]] == ["ai", "tool", "tool"]


async def test_a_compacting_call_discards_the_findings_of_its_batch(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: InterruptEvent,
    monkeypatch: pytest.MonkeyPatch,
    database_gate: ProcessDbGate,
) -> None:
    from base.config import settings

    monkeypatch.setattr(settings.agent, "security_scan_enabled", True)

    result = await _run_calls(
        _state(
            _scan("web.fetch"),
            _scan("shell.run") + "from base.agents.lifecycle import SystemHalt\nraise SystemHalt()",
        ),
        _runtime(hosted_resources=hosted_resources, database_gate=database_gate),
        _config(),
        process=SubprocessExecProcess(),
    )

    assert result["security_findings"] == []


async def test_unknown_tool_does_not_consume_sibling_code(
    hosted_resources: HostedTurnResources,
) -> None:
    state = _state("print('must not run')", "print('runs')")
    ai = state.messages[-1]
    assert isinstance(ai, AIMessage)
    ai.tool_calls[0]["name"] = "meiva.files.edit"
    process = ScriptedExecProcess(
        [(ExecDone(output="runs"), ResultPayload(kind="done", state_update={}, sdk_calls=[]))]
    )

    result = await _run_calls(
        state,
        _node_runtime(
            hosted_resources=hosted_resources,
        ),
        {"configurable": {"thread_id": "7"}},
        process=process,
    )
    assert result is not None
    _, first, second = result["messages"]
    assert "unknown tool" in first.content
    assert "runs" in second.content
    assert len(process.calls) == 1
    assert process.calls[0].code == "print('runs')"


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
    hosted_resources: HostedTurnResources,
    fake_cancel_event: InterruptEvent,
    database_gate: ProcessDbGate,
) -> None:
    state = AgentState(messages=[_ai_with_two_content_tool_uses()])
    result = await _run_calls(
        state,
        _runtime(hosted_resources=hosted_resources, database_gate=database_gate),
        _config(),
        process=SubprocessExecProcess(),
    )
    assert result is not None
    fixed_ai, first, second = result["messages"]
    assert len(fixed_ai.tool_calls) == 2
    assert first.tool_call_id == "call_00"
    assert second.tool_call_id == "call_01"
    assert "skills" in first.content
    assert "agents" in second.content


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


async def test_exec_node_feeds_back_on_unknown_tool_name(
    hosted_resources: HostedTurnResources, database_gate: ProcessDbGate
) -> None:
    state = AgentState(messages=[_ai_with_hallucinated_tool_name()])

    result = await _run_calls(
        state,
        _runtime(hosted_resources=hosted_resources, database_gate=database_gate),
        _config(),
        process=SubprocessExecProcess(),
    )

    assert result is not None
    assert result["halted"] is False
    _, tool_msg = result["messages"]
    assert tool_msg.tool_call_id == "call_99"
    assert "meiva.files.edit" in tool_msg.content
    assert "execute_code" in tool_msg.content


async def test_exec_node_feeds_back_when_code_key_missing(
    hosted_resources: HostedTurnResources, database_gate: ProcessDbGate
) -> None:
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
        _runtime(hosted_resources=hosted_resources, database_gate=database_gate),
        _config(),
        process=SubprocessExecProcess(),
    )

    assert result is not None
    _, tool_msg = result["messages"]
    assert tool_msg.tool_call_id == "call_88"
    assert "execute_code" in tool_msg.content


async def test_langgraph_owns_each_call_state_transition(
    hosted_resources: HostedTurnResources,
) -> None:
    from typing import Annotated

    def append_digit(current: int, digit: int) -> int:
        return current * 10 + digit

    class DecimalState(AgentState):
        total: Annotated[int, append_digit] = 0

    process = ScriptedExecProcess(
        [
            (ExecDone(output="ok"), ResultPayload(kind="done", state_update={"total": 1})),
            (ExecDone(output="ok"), ResultPayload(kind="done", state_update={"total": 2})),
        ]
    )

    state = DecimalState(total=7, messages=_state("first()", "second()").messages)
    result = await _run_calls(
        state,
        _node_runtime(
            hosted_resources=hosted_resources,
        ),
        _config(),
        process=process,
    )
    assert [call.state["total"] for call in process.calls] == [7, 71]
    assert result["total"] == 712


async def test_checkpoint_resume_keeps_results_and_deferred_notes(
    hosted_resources: HostedTurnResources,
    fake_cancel_event: InterruptEvent,
) -> None:
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    note = HumanMessage(content="context from first call", id="note-first")
    process = ScriptedExecProcess(
        [
            (
                ExecDone(output="first"),
                ResultPayload(kind="done", state_update={"messages": [note]}, sdk_calls=None),
            ),
            (ExecDone(output="second"), ResultPayload(kind="done", state_update={}, sdk_calls=[])),
        ]
    )

    graph = _graph(
        _messages_state_class(),
        checkpointer=InMemorySaver(),
        interrupt_after=["exec"],
        process=process,
    )
    config = _config()
    runtime = _node_runtime(
        hosted_resources=hosted_resources,
    )
    await graph.ainvoke(dict(_state("first()", "second()")), config=config, context=runtime.context)
    checkpoint = await graph.aget_state(config)
    assert checkpoint.next == ("exec",)
    assert len(checkpoint.values["messages"]) == 2
    assert checkpoint.values["messages"][-1].tool_call_id == "call-0"
    assert checkpoint.values["pending_exec_notes"] == [note]
    assert len(process.calls) == 1

    result = await graph.ainvoke(None, config=config, context=runtime.context)
    assert [call.code for call in process.calls] == ["first()", "second()"]
    assert [message.tool_call_id for message in result["messages"][1:3]] == [
        "call-0",
        "call-1",
    ]
    assert result["messages"][3:] == [note]
    assert result["pending_exec_notes"] == []
    first, second = result["messages"][1:3]
    assert "sdk_calls" not in read_meiva_kwargs(first)
    assert read_meiva_kwargs(second).get("sdk_calls") == []
    for call in process.calls:
        assert call.context is runtime.context
        assert call.config_overlay == runtime.context.require_agent().overlay()
        assert isinstance(call.chunks, ExecOutputChunkPublisher)
        assert call.cancel_event is fake_cancel_event
    assert process.calls[0].chunks is not process.calls[1].chunks
    assert process.calls[0].state["pending_exec_notes"] == []
    assert process.calls[1].state["pending_exec_notes"] == [note.model_dump()]
