import json
import random
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.graph.exec.node import exec_node_impl
from agent.graph.exec.process import SubprocessExecProcess
from agent.graph.exec.tests.process_inputs import live_parent_facts
from agent.graph.interrupt import InterruptEvent
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import read_meiva_kwargs
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import idle_publisher
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.model_catalog import inert_keys
from base.paths import meiva_home, resolve_meiva_home
from meiva.tests.fixtures.pin_agent import snapshot_clients
from meiva_builtins.plugins.meiva_syntax_fix.agent_runtime import syntax_fix_before_exec
from meiva_builtins.plugins.meiva_syntax_fix.tests.hook_support import (
    UNAVAILABLE,
    agent_slices,
    execute_code_state,
)


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


def _graph(state_cls: type[AgentState], **compile_options: Any) -> Any:
    from langgraph.graph import END, START, StateGraph

    builder: Any = StateGraph(state_cls, context_schema=MeivaContext)
    builder.add_node(
        "exec",
        partial(exec_node_impl, process=SubprocessExecProcess(live_parent_facts(), rng=_rng())),
        input_schema=state_cls,
    )
    builder.add_node("after_exec", lambda _state: {})
    builder.add_edge(START, "exec")
    builder.add_edge("after_exec", END)
    return builder.compile(**compile_options)


async def _run_calls(
    state: AgentState, runtime: Runtime[MeivaContext], config: RunnableConfig
) -> dict[str, Any]:
    return await _graph(type(state)).ainvoke(dict(state), context=runtime.context, config=config)


@pytest.fixture
def runtime_for(database_gate: ProcessDbGate) -> Iterator[Callable[..., Runtime[MeivaContext]]]:
    clients = snapshot_clients()

    def build(*, llm_override: str | None = None, **pins: Any) -> Runtime[MeivaContext]:
        return Runtime(
            context=MeivaContext(
                meiva_home=resolve_meiva_home(),
                user_home=Path.home(),
                event_publisher=idle_publisher(),
                agent=agent_slices(llm_override=llm_override, **pins),
                db=Database.from_settings(gate=database_gate),
                bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
                clients=clients,
                clock_factory=Clock.from_settings,
                catalog=build_model_catalog(home=meiva_home()),
                provider_keys=inert_keys(build_model_catalog(home=meiva_home())),
                identity=AgentIdentity(agent_id=7, owns_loop=True),
            )
        )

    try:
        yield build
    finally:
        clients.close()


async def test_syntax_repair_is_per_call_and_preserves_content(
    runtime_for: Callable[..., Runtime[MeivaContext]],
) -> None:
    state = execute_code_state("print(1\uff0c1)", "print(2\uff0c2)")
    ai = state.messages[-1]
    assert isinstance(ai, AIMessage)
    ai.content = [
        {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]}
        for call in ai.tool_calls
    ]
    original = ai.model_dump()

    update = await syntax_fix_before_exec(state, runtime_for(syntax_fix_ruff_format=False), {})

    assert update is not None
    fixed = update["messages"][0]
    assert [call["id"] for call in fixed.tool_calls] == ["call-0", "call-1"]
    assert [call["args"]["code"] for call in fixed.tool_calls] == ["print(1,1)", "print(2,2)"]
    assert [block["input"]["code"] for block in fixed.content] == ["print(1,1)", "print(2,2)"]
    assert ai.model_dump() == original


async def test_unfixable_syntax_does_not_skip_sibling(
    fake_cancel_event: InterruptEvent,
    runtime_for: Callable[..., Runtime[MeivaContext]],
) -> None:
    from agent.messages.guard import guarded_add_messages

    runtime = runtime_for(llm_override=UNAVAILABLE)
    state = execute_code_state("return", 'print("sibling ran")')

    update = await syntax_fix_before_exec(state, runtime, {})

    assert update is not None
    assert "goto" not in update
    state.messages = guarded_add_messages(state.messages, update["messages"])
    result = await _run_calls(state, runtime, {"configurable": {"thread_id": "7"}})
    assert result is not None
    _, first, second = result["messages"]
    assert "SyntaxError" in first.content
    assert "sibling ran" in second.content


async def test_unfixable_single_call_error_result_carries_creation_time(
    runtime_for: Callable[..., Runtime[MeivaContext]],
) -> None:
    before = datetime.now(UTC)

    update = await syntax_fix_before_exec(
        execute_code_state("return"), runtime_for(llm_override=UNAVAILABLE), {}
    )

    assert update is not None
    assert update["goto"] == "after_exec"
    _, error = update["messages"]
    assert isinstance(error, ToolMessage)
    kwargs = read_meiva_kwargs(error)
    assert kwargs.get("meiva_msg_type") == "exec_output"
    created = kwargs.get("meiva_created_at")
    assert created is not None
    assert before <= datetime.fromisoformat(created) <= datetime.now(UTC)


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


async def test_syntax_fix_preserves_recovered_calls(
    runtime_for: Callable[..., Runtime[MeivaContext]],
) -> None:
    state = AgentState(messages=[_ai_with_two_content_tool_uses()])
    update = await syntax_fix_before_exec(state, runtime_for(), _config())
    assert update is not None
    fixed = update["messages"][0]
    assert [call["id"] for call in fixed.tool_calls] == ["call_00", "call_01"]
    assert [call["args"]["code"].strip() for call in fixed.tool_calls] == [
        'print("skills")',
        'print("agents")',
    ]
