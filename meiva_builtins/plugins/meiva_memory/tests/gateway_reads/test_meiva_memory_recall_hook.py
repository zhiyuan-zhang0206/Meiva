# meiva:note
# The passive-recall hook's trigger gate and per-window record (`plugins/meiva_memory/agent_runtime.py`).
# The hook runs for real against a gateway on a port-0 listener and note files under a unit home, so
# the gate shows as the requests the gateway did or did not receive. The search and filter pipeline
# is owned by `agent/graph/tests/prompt/test_memory_recall.py`.
from __future__ import annotations

import threading
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from agent.messages import inbound_message
from agent.state import CompactState, MemoryState, build_agent_state
from base.agents.context import MeivaContext
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import EMPTY
from base.paths import memory_dir
from base.paths.home import resolve_meiva_home
from meiva.tests.fixtures.gateway_wire import GatewayWire, Reply
from meiva_builtins.plugins.meiva_memory import agent_runtime

pytestmark = pytest.mark.usefixtures("unit_home")

_SEARCH = "/api/memory/search"
_HOOK = agent_runtime.passive_memory_recall_before_llm


def _hit(path: str) -> dict[str, object]:
    return {"path": path, "description": "", "tags": [], "score": 0.9, "chunk": ""}


def _answer(wire: GatewayWire, *paths: str) -> None:
    for path in paths:
        note = memory_dir() / path
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("note", encoding="utf-8")
    wire.stub.reply("POST", _SEARCH, {"results": [_hit(path) for path in paths]})


def _state(messages: list[AnyMessage], **fields: Any):
    return build_agent_state(EMPTY)(messages=messages, **fields)


def _runtime(catalog: ModelCatalog, wire: GatewayWire, **pins: Any) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            agent=AgentSlices.resolve(
                {"passive_memory_recall_enabled": True, **pins}, default_reader=defaults()
            ),
            catalog=catalog,
            clients=wire.context.clients,
            clock_factory=Clock.from_settings,
            meiva_home=resolve_meiva_home(),
        )
    )


def _inbound(source: str) -> AnyMessage:
    return inbound_message(content="hello", source=source, inbound_id=1, body_start=0)


def _excluded(wire: GatewayWire) -> list[set[str]]:
    return [set(request.body.get("exclude", [])) for request in wire.stub.received]


@pytest.mark.parametrize(
    "source",
    ["user", "agent:402", "schedule:7", "system"],
    ids=["user", "agent", "schedule", "system"],
)
async def test_fires_on_real_inbound_sources(
    source: str, model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    _answer(wire, "a.md")
    state = _state([AIMessage(content="prev", id="a0"), _inbound(source)])

    result = await _HOOK(state, _runtime(model_catalog, wire), RunnableConfig())

    assert len(wire.stub.received) == 1
    assert result is not None
    assert "a.md" in result["memory"].injected_paths


@pytest.mark.parametrize(
    "source",
    ["watcher:3", "shell:9"],
    ids=["watcher", "shell"],
)
async def test_skips_machine_wakeups(
    source: str, model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    _answer(wire, "a.md")
    state = _state([AIMessage(content="prev", id="a0"), _inbound(source)])

    result = await _HOOK(state, _runtime(model_catalog, wire), RunnableConfig())

    assert wire.stub.received == []
    assert result is None


async def test_fires_when_a_real_inbound_sits_among_wakeups(
    model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    _answer(wire, "a.md")
    state = _state(
        [
            AIMessage(content="prev", id="a0"),
            _inbound("watcher:3"),
            _inbound("agent:402"),
        ]
    )

    result = await _HOOK(state, _runtime(model_catalog, wire), RunnableConfig())

    assert len(wire.stub.received) == 1
    assert result is not None


async def test_gateway_error_leaves_the_turn_running(
    model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    wire.stub.reply("POST", _SEARCH, "Server error", status=500)
    state = _state([AIMessage(content="prev", id="a0"), _inbound("user")])

    result = await _HOOK(state, _runtime(model_catalog, wire), RunnableConfig())

    assert result is None
    assert len(wire.stub.received) == 1


async def test_recall_deadline_exceeded_skips_recall_this_turn(
    model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    release = threading.Event()
    wire.stub.hold("POST", _SEARCH, release, {"results": [_hit("late.md")]})
    state = _state([AIMessage(content="prev", id="a0"), _inbound("user")])
    runtime = _runtime(model_catalog, wire, memory_recall_deadline_seconds=0.05)

    try:
        assert await _HOOK(state, runtime, RunnableConfig()) is None
    finally:
        release.set()


async def test_no_inbound_tail_is_a_noop(model_catalog: ModelCatalog, wire: GatewayWire) -> None:
    _answer(wire, "a.md")
    state = _state([AIMessage(content="idle", id="a0")])

    result = await _HOOK(state, _runtime(model_catalog, wire), RunnableConfig())

    assert wire.stub.received == []
    assert result is None


async def test_a_disabled_feature_never_searches(
    model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    _answer(wire, "a.md")
    state = _state([AIMessage(content="prev", id="a0"), _inbound("user")])
    runtime = _runtime(model_catalog, wire, passive_memory_recall_enabled=False)

    assert await _HOOK(state, runtime, RunnableConfig()) is None
    assert wire.stub.received == []


async def test_the_record_is_kept_while_the_window_is_unchanged(
    model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    _answer(wire, "a.md")
    state = _state(
        [AIMessage(content="prev", id="a0"), _inbound("user")],
        memory=MemoryState(injected_paths={"seen.md"}, window=1),
        compact=CompactState(version=1),
    )

    result = await _HOOK(state, _runtime(model_catalog, wire), RunnableConfig())

    assert _excluded(wire) == [{"seen.md"}]
    assert result is not None
    assert result["memory"] == MemoryState(injected_paths={"a.md"}, window=1)


async def test_a_compaction_summarizes_the_injected_notes_away_so_none_is_excluded(
    model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    _answer(wire, "a.md")
    state = _state(
        [AIMessage(content="prev", id="a0"), _inbound("user")],
        memory=MemoryState(injected_paths={f"note-{i}.md" for i in range(2088)}),
        compact=CompactState(version=3),
    )

    result = await _HOOK(state, _runtime(model_catalog, wire), RunnableConfig())

    assert _excluded(wire) == [set()]
    assert result is not None
    assert result["memory"] == MemoryState(injected_paths={"a.md"}, window=3)


async def test_a_compaction_resets_the_record_even_when_nothing_is_recalled(
    model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    wire.stub.reply("POST", _SEARCH, {"results": []})
    state = _state(
        [AIMessage(content="prev", id="a0"), _inbound("user")],
        memory=MemoryState(injected_paths={"old.md"}, window=0),
        compact=CompactState(version=2),
    )

    result = await _HOOK(state, _runtime(model_catalog, wire), RunnableConfig())

    assert _excluded(wire) == [set()]
    assert result == {"memory": MemoryState(window=2)}


async def test_a_compaction_resets_the_record_on_a_turn_that_does_not_recall(
    model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    state = _state(
        [AIMessage(content="prev", id="a0"), _inbound("watcher:3")],
        memory=MemoryState(injected_paths={"old.md"}, window=0),
        compact=CompactState(version=1),
    )

    result = await _HOOK(state, _runtime(model_catalog, wire), RunnableConfig())

    assert wire.stub.received == []
    assert result == {"memory": MemoryState(window=1)}


async def test_after_a_compaction_only_notes_injected_since_are_excluded(
    model_catalog: ModelCatalog, wire: GatewayWire
) -> None:
    for name in ("a.md", "b.md", "c.md"):
        (memory_dir() / name).parent.mkdir(parents=True, exist_ok=True)
        (memory_dir() / name).write_text("note", encoding="utf-8")
    wire.stub.reply_in_turn(
        "POST",
        _SEARCH,
        [Reply(200, {"results": [_hit(name)]}) for name in ("a.md", "b.md", "c.md")],
    )
    runtime = _runtime(model_catalog, wire)
    state_cls = build_agent_state(EMPTY)

    async def recall_pass(state: Any) -> dict[str, Any] | None:
        return await _HOOK(state, runtime, RunnableConfig())

    async def compaction(_state: Any) -> dict[str, Any]:
        return {"compact": CompactState(version=1)}

    graph: Any = StateGraph(state_cls)
    steps = [
        ("recall_one", recall_pass),
        ("compact", compaction),
        ("recall_two", recall_pass),
        ("recall_three", recall_pass),
    ]
    previous = START
    for name, node in steps:
        graph.add_node(name, node)
        graph.add_edge(previous, name)
        previous = name
    graph.add_edge(previous, END)

    final = state_cls.model_validate(
        await graph.compile().ainvoke(
            state_cls(messages=[AIMessage(content="prev", id="a0"), _inbound("user")])
        )
    )

    assert _excluded(wire) == [set(), set(), {"b.md"}]
    assert final.memory == MemoryState(injected_paths={"b.md", "c.md"}, window=1)
