"""Real graph persistence distinguishes new messages from legacy replay UUIDs."""

from collections.abc import Mapping
from typing import Annotated, Protocol, TypedDict

import pytest
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langgraph.channels import DeltaChannel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Durability

from agent.hooks import merge_result
from agent.messages.guard import guarded_add_messages, guarded_delta_reducer
from agent.state import BaseAgentState
from base.agents.history.timeline import build_timeline_items
from base.agents.messages.identity import normalize_stored_message_ids
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.agents.tests.langgraph_views import NodeGraphBuilder, update_messages
from base.clock import Clock
from base.config import settings
from base.host.boundary.json_values import is_object_list

_TIMELINE_INPUTS = TimestampPolicy.from_clock(
    Clock.from_settings(), timestamps_enabled=settings.general.message_timestamps
)


class _State(TypedDict):
    messages: Annotated[
        list[AnyMessage], DeltaChannel(guarded_delta_reducer, snapshot_frequency=1000)
    ]


class _Snapshot(Protocol):
    @property
    def values(self) -> Mapping[str, object]: ...


class _CompiledGraph(Protocol):
    def invoke(
        self, input: _State, config: RunnableConfig, *, durability: Durability
    ) -> object: ...

    def get_state(self, config: RunnableConfig) -> _Snapshot: ...


type _GraphBuilder = NodeGraphBuilder[Runnable[_State, object], _CompiledGraph]


def _new_builder() -> _GraphBuilder:
    return StateGraph(_State)


def _stored_messages(app: _CompiledGraph, config: RunnableConfig) -> list[BaseMessage]:
    return update_messages(app.get_state(config).values)


@pytest.mark.parametrize("durability", ["sync", "async", "exit"])
def test_graph_input_node_and_working_copy_output_have_persisted_ids(
    durability: Durability,
) -> None:
    def reply(state: _State) -> dict[str, object]:
        direct = AIMessage(content="same text")
        working = AIMessage(content="same text")
        # Plugin working-copy/hook co-write uses the single-merge guard before
        # put_writes. It must not mark these newly created deltas as legacy.
        guarded_add_messages(state["messages"], [working])
        update: dict[str, object] = {}
        writers: dict[str, str] = {}
        for name in ("first", "second"):
            merge_result(
                update,
                writers,
                {"messages": [AIMessage(content="same text")]},
                name,
                "before_turn",
                BaseAgentState.model_fields,
            )
        merged = update["messages"]
        assert is_object_list(merged)
        return {"messages": [direct, working, *merged]}

    graph = _new_builder()
    graph.add_node("reply", RunnableLambda(reply))
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    saver = InMemorySaver()
    app = graph.compile(saver)
    config: RunnableConfig = {"configurable": {"thread_id": f"identity-{durability}"}}
    app.invoke({"messages": [HumanMessage(content="new input")]}, config, durability=durability)
    first = _stored_messages(app, config)
    second = _stored_messages(app, config)
    assert len(first) == 5
    assert len({m.id for m in first}) == 5
    assert all(m.id and not m.additional_kwargs.get("meiva_ephemeral_message_id") for m in first)
    assert [m.id for m in first] == [m.id for m in second]
    items, _ = build_timeline_items(first, inputs=_TIMELINE_INPUTS)
    assert [i.source_message_id for i in items] == [m.id for m in first]


def test_legacy_read_marker_precedes_working_merge_and_survives_reducer() -> None:
    raw = AIMessage(content="legacy seed")
    stored = normalize_stored_message_ids([raw])
    guarded_add_messages(stored, [HumanMessage(content="new working delta")])
    assert raw.id is not None
    replayed = guarded_delta_reducer([], [stored])
    item = build_timeline_items(replayed, inputs=_TIMELINE_INPUTS)[0][0]
    assert item.source_message_id is None
    assert raw.additional_kwargs["meiva_ephemeral_message_id"] is True
