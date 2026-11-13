from __future__ import annotations

import os
import random

# meiva:note
# The compiled graph's shape, from both ends: every node receives the plugins' state
# channels (a node annotated with the static base class would silently drop them), and
# an agent on an external harness reaches `harness_turn` instead of the native turn
# nodes. Both run a real compiled graph rather than reading `_build.py`.
# meiva:link agent/graph/docs/graph.md
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

import agent.state as agent_state
from agent.graph import build_graph
from agent.graph.claim.node import route_target
from agent.graph.claim.tests.harness_support import FakeEngine
from agent.graph.claim.tests.harness_support import runtime as record_runtime
from agent.graph.exec.tests.process_inputs import live_parent_facts
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState, ContextReset
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import Harness
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import EMPTY, ExtensionRegistry, PluginContributions
from base.paths import meiva_home, resolve_meiva_home


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


class _Counter(BaseModel):
    seen: int = 0


def test_every_node_receives_the_plugins_state_channels() -> None:
    registry = ExtensionRegistry((("demo", PluginContributions(state=(_Counter,))),))

    graph = build_graph(None, registry, parent=live_parent_facts(), rng=_rng())

    state_cls = graph.builder.state_schema
    assert state_cls is not agent_state.BaseAgentState
    assert "demo__seen" in state_cls.model_fields
    assert graph.builder.nodes, "the graph has nodes"
    for name, spec in graph.builder.nodes.items():
        assert spec.input_schema is state_cls, name
    assert agent_state.AgentState is agent_state.BaseAgentState


NATIVE_NODES = frozenset({"llm", "exec"})


async def _empty_stream(*_a: object, **_kw: object) -> AsyncIterator[Any]:
    for item in ():
        yield item


def make_context(harness: Harness, engine: FakeEngine | None) -> MeivaContext:
    llm = MagicMock()
    llm.bind_tools.return_value = llm
    llm.astream.side_effect = _empty_stream
    return MeivaContext(
        meiva_home=resolve_meiva_home(),
        environ=os.environ,
        harness=harness,
        harness_bridge=engine,
        llm=llm,
        rng=_rng(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        catalog=build_model_catalog(home=meiva_home()),
        clock_factory=Clock.from_settings,
    )


@pytest.fixture
def recorded_graph(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate, event_bus: EventBus
) -> tuple[int, Callable[[FakeEngine], MeivaContext]]:
    agent_id = agent_row(db_conn)
    _ = db_conn.execute("UPDATE agents_meta SET harness='antigravity' WHERE id=%s", (agent_id,))
    db_conn.commit()

    def context(engine: FakeEngine) -> MeivaContext:
        wired = record_runtime(
            engine, agent_id=agent_id, database_gate=database_gate, event_bus=event_bus
        ).context
        return replace(
            make_context(Harness.ANTIGRAVITY, engine),
            db=wired.db,
            bus=event_bus,
            environ=wired.require_environ(),
        )

    return agent_id, context


async def executed_nodes(
    harness: Harness,
    engine: FakeEngine | None,
    *,
    stop_at: frozenset[str] = frozenset(),
    context: MeivaContext | None = None,
    agent_id: int = 6984,
) -> list[str]:
    graph = build_graph(None, EMPTY, parent=live_parent_facts(), rng=_rng())
    state = AgentState(
        messages=[
            SystemMessage(content="prompt"),
            HumanMessage(
                content="do it",
                additional_kwargs={"meiva_msg_type": "inbound", "meiva_inbound_id": 7},
            ),
        ],
        harness_inputs=(
            [{"key": "k1", "prompt": "do it", "timeline_items": []}]
            if harness is not Harness.MEIVA
            else []
        ),
    )
    seen: list[str] = []
    stream = _stream(graph, state, context or make_context(harness, engine), agent_id=agent_id)
    async for chunk in stream:
        seen.extend(chunk.keys())
        if stop_at.intersection(seen):
            break
    return seen


# meiva:note
# `astream` is typed as an overload set that is partially unknown under this repo's
# strict test rules; the one ignore keeps the driver readable at the call site.
# meiva:link agent/graph/docs/graph.md
def _stream(
    graph: Any, state: AgentState, context: MeivaContext, *, agent_id: int = 6984
) -> AsyncIterator[dict[str, Any]]:
    return graph.astream(  # pyright: ignore[reportUnknownMemberType]
        state,
        {"configurable": {"thread_id": str(agent_id)}},
        context=context,
        stream_mode="updates",
    )


async def test_unmarked_harness_reply_gets_one_correction_turn(
    recorded_graph: tuple[int, Callable[[FakeEngine], MeivaContext]],
) -> None:
    agent_id, context = recorded_graph
    engine = FakeEngine(reply="Unmarked answer.")

    seen = await executed_nodes(
        Harness.ANTIGRAVITY, engine, context=context(engine), agent_id=agent_id
    )

    [(original, _), (correction, _)] = engine.submitted
    assert original == "do it"
    assert "That text was folded into Details" in correction
    assert seen.count("harness_turn") == 1, "the correction is delivered inside the same invocation"


async def test_an_external_harness_never_enters_the_native_turn_nodes(
    recorded_graph: tuple[int, Callable[[FakeEngine], MeivaContext]],
) -> None:
    agent_id, context = recorded_graph
    engine = FakeEngine()

    seen = await executed_nodes(
        Harness.ANTIGRAVITY, engine, context=context(engine), agent_id=agent_id
    )

    assert "harness_turn" in seen, "the harness node is the turn"
    assert not NATIVE_NODES.intersection(seen), f"native turn nodes ran: {seen}"
    [(prompt, _)] = engine.submitted
    assert prompt == "do it"


# meiva:note
# Stopped at llm on purpose: the native loop needs tool calls and a subprocess to finish,
# and none of that is what this test is about. Seeing the native verdict reach its own
# node is the control that the harness run's empty native set is not an empty run.
# meiva:link agent/graph/docs/graph.md
async def test_the_built_in_loop_never_enters_the_harness_node() -> None:
    seen = await executed_nodes(Harness.MEIVA, None, stop_at=frozenset({"llm"}))

    assert "before_turn" in seen
    assert "llm" in seen, "the built-in loop still runs its own nodes"
    assert "harness_turn" not in seen


# meiva:note
# The compaction path parks its destination in `ContextReset.resume` and lets
# `init_context` jump there, so the harness rewrite has to happen at `before_turn` too:
# routing only claim's verdicts left a compacted external-harness agent resuming into
# `llm`, where the native model answered and the CLI session sat idle.
# meiva:link agent/graph/docs/harness-turn.md
async def test_a_context_reset_keeps_an_external_harness_off_the_native_nodes(
    recorded_graph: tuple[int, Callable[[FakeEngine], MeivaContext]],
) -> None:
    agent_id, context = recorded_graph
    engine = FakeEngine()
    state = AgentState(
        messages=[],
        context_reset=ContextReset(
            resume="before_turn", tail=[HumanMessage(content="the compacted summary")]
        ),
    )
    seen: list[str] = []
    async for chunk in _stream(
        build_graph(None, EMPTY, parent=live_parent_facts(), rng=_rng()),
        state,
        context(engine),
        agent_id=agent_id,
    ):
        seen.extend(chunk.keys())

    assert "init_context" in seen
    assert "before_turn" in seen
    assert "harness_turn" in seen, "the reset resumes into the harness node"
    assert len(engine.submitted) == 1
    assert f"# Role & Operating Context — Agent #{agent_id}" in engine.submitted[0][0]
    assert engine.submitted[0][0].endswith("\n\nthe compacted summary")
    assert not NATIVE_NODES.intersection(seen), f"native turn nodes ran: {seen}"


def test_route_target_rewrites_only_the_work_verdict() -> None:
    external = make_context(Harness.ANTIGRAVITY, FakeEngine())
    native = make_context(Harness.MEIVA, None)

    assert route_target(external, "llm") == "harness_turn"
    assert route_target(native, "llm") == "llm"
    for goto in ("before_turn", "claim", "__end__", "init_context", "harness_turn"):
        assert route_target(external, goto) == goto
