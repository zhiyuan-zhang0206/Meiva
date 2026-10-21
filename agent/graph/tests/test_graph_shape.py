from __future__ import annotations

# meiva:note
# The compiled graph's shape, from both ends: every node receives the plugins' state
# channels (a node annotated with the static base class would silently drop them), and
# an agent on an external harness reaches `harness_turn` instead of the native turn
# nodes. Both run a real compiled graph rather than reading `_build.py`.
# meiva:link agent/graph/docs/graph.meiva.okf.md
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

import agent.state as agent_state
from agent.graph import build_graph
from agent.graph.claim.node import route_target
from agent.state import AgentState, ContextReset
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import Harness
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import HarnessTurn, Usage
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY, ExtensionRegistry, PluginContributions


class _Counter(BaseModel):
    seen: int = 0


def test_every_node_receives_the_plugins_state_channels() -> None:
    registry = ExtensionRegistry((("demo", PluginContributions(state=(_Counter,))),))

    graph = build_graph(None, registry)

    state_cls = graph.builder.state_schema
    assert state_cls is not agent_state.BaseAgentState
    assert "demo__seen" in state_cls.model_fields
    assert graph.builder.nodes, "the graph has nodes"
    for name, spec in graph.builder.nodes.items():
        assert spec.input_schema is state_cls, name
    assert agent_state.AgentState is agent_state.BaseAgentState


NATIVE_NODES = frozenset({"before_llm", "llm", "before_exec", "exec", "after_exec"})


class FakeEngine:
    # meiva:note
    # A turn engine that answers at once. The graph test drives the real compiled
    # graph — real claim, real init_context, real routing — and swaps only the
    # provider session, which is the one thing a test cannot run.
    # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
    def __init__(self) -> None:
        self.harness = Harness.CLAUDE_CODE
        self.model = "claude-sonnet-5-5"
        self.submitted: list[str] = []

    async def run_turn(self, text: str, *, key: str) -> HarnessTurn:
        self.submitted.append(text)
        return HarnessTurn(
            turn_id="t1",
            text="the harness answered",
            usage=Usage(input_tokens=1, output_tokens=1),
            cost_usd=0.0,
            subtype="success",
            is_error=False,
            interrupted=False,
        )

    async def interrupt(self) -> object:
        return object()

    async def close(self, *, reason: str = "closed") -> None:
        return None


def make_context(harness: Harness, engine: FakeEngine | None) -> MeivaContext:
    return MeivaContext(
        harness=harness,
        harness_bridge=engine,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )


async def executed_nodes(
    harness: Harness, engine: FakeEngine | None, *, stop_at: frozenset[str] = frozenset()
) -> list[str]:
    graph = build_graph(None, EMPTY)
    state = AgentState(
        messages=[
            SystemMessage(content="prompt"),
            HumanMessage(
                content="do it",
                additional_kwargs={"meiva_msg_type": "inbound", "meiva_inbound_id": 7},
            ),
        ],
        harness_cursor=1,
    )
    seen: list[str] = []
    stream = _stream(graph, state, make_context(harness, engine))
    async for chunk in stream:
        seen.extend(chunk.keys())
        if stop_at.intersection(seen):
            break
    return seen


# meiva:note
# `astream` is typed as an overload set that is partially unknown under this repo's
# strict test rules; the one ignore keeps the driver readable at the call site.
# meiva:link agent/graph/docs/graph.meiva.okf.md
def _stream(graph: Any, state: AgentState, context: MeivaContext) -> AsyncIterator[dict[str, Any]]:
    return graph.astream(  # pyright: ignore[reportUnknownMemberType]
        state,
        {"configurable": {"thread_id": "6984"}},
        context=context,
        stream_mode="updates",
    )


async def test_an_external_harness_never_enters_the_native_turn_nodes() -> None:
    engine = FakeEngine()

    seen = await executed_nodes(Harness.CLAUDE_CODE, engine)

    assert "harness_turn" in seen, "the harness node is the turn"
    assert not NATIVE_NODES.intersection(seen), f"native turn nodes ran: {seen}"
    assert engine.submitted == ["do it"], "the message reached the harness once"


# meiva:note
# Stopped at before_llm on purpose: the native loop needs a model, tool calls and a
# subprocess to finish, and none of that is what this test is about. Seeing the native
# verdict reach its own node is the control that the harness run's empty native set is
# not an empty run.
# meiva:link agent/graph/docs/graph.meiva.okf.md
async def test_the_built_in_loop_never_enters_the_harness_node() -> None:
    seen = await executed_nodes(Harness.MEIVA, None, stop_at=frozenset({"before_llm"}))

    assert "before_llm" in seen, "the built-in loop still runs its own nodes"
    assert "harness_turn" not in seen


# meiva:note
# The compaction path parks its destination in `ContextReset.resume` and lets
# `init_context` jump there, so the harness rewrite has to happen at that jump too:
# routing only claim's verdicts left a compacted external-harness agent resuming into
# `before_llm`, where the native model answered and the CLI session sat idle.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def test_a_context_reset_keeps_an_external_harness_off_the_native_nodes() -> None:
    engine = FakeEngine()
    state = AgentState(
        messages=[],
        context_reset=ContextReset(
            resume="before_llm", tail=[HumanMessage(content="the compacted summary")]
        ),
    )
    seen: list[str] = []
    async for chunk in _stream(
        build_graph(None, EMPTY), state, make_context(Harness.CLAUDE_CODE, engine)
    ):
        seen.extend(chunk.keys())

    assert "init_context" in seen
    assert "harness_turn" in seen, "the reset resumes into the harness node"
    assert engine.submitted == ["the compacted summary"]
    assert not NATIVE_NODES.intersection(seen), f"native turn nodes ran: {seen}"


def test_route_target_rewrites_only_the_work_verdict() -> None:
    external = make_context(Harness.CLAUDE_CODE, FakeEngine())
    native = make_context(Harness.MEIVA, None)

    assert route_target(external, "before_llm") == "harness_turn"
    assert route_target(native, "before_llm") == "before_llm"
    for goto in ("claim", "__end__", "init_context", "harness_turn"):
        assert route_target(external, goto) == goto
