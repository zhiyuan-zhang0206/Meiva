"""Messages channel: DeltaChannel form since the write switch (task #3180).

Pins:
1. Annotation normalization - `add_messages` (plugin-declared spelling),
   `guarded_add_messages` (pre-switch channel reducer) and the delta form
   (`DeltaChannel(guarded_delta_reducer, ...)`) are one contract; plugin
   registration keeps accepting the declared spelling.
2. Reducer resolution - the delta form resolves to the guarded single-merge,
   never the last-value fallback (a silent full overwrite of `messages`).
3. A real LangGraph run over the flipped channel stores delta artifacts
   (per-super-step writes + periodic `_DeltaSnapshot` cadence) and folds at
   read time on resume.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Protocol, TypedDict, get_args

from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from agent.messages.guard import guarded_add_messages, guarded_delta_reducer
from agent.state import (
    MESSAGES_DELTA_CHANNEL,
    MESSAGES_DELTA_SNAPSHOT_FREQUENCY,
    BaseAgentState,
    accumulate_delta,
    is_messages_reducer_form,
    messages_annotation_key,
    resolve_reducer,
)
from base.agents.tests.langgraph_views import AsyncGraph, NodeGraphBuilder
from base.host.boundary.json_values import is_object_list
from base.host.boundary.untyped import object_mapping, object_sequence

PLUGIN_SPELLING = Annotated[list[AnyMessage], add_messages]
GUARDED_SPELLING = Annotated[list[AnyMessage], guarded_add_messages]


def test_base_annotation_is_the_delta_form() -> None:
    annotation = object_mapping(BaseAgentState.__annotations__)["messages"]
    meta = object_sequence(get_args(annotation))[1:]
    assert len(meta) == 1
    channel = meta[0]
    assert isinstance(channel, DeltaChannel)
    assert channel.reducer is guarded_delta_reducer
    assert channel.snapshot_frequency == MESSAGES_DELTA_SNAPSHOT_FREQUENCY


def test_annotation_keys_equivalent_across_spellings() -> None:
    base_key = messages_annotation_key(object_mapping(BaseAgentState.__annotations__)["messages"])
    assert messages_annotation_key(PLUGIN_SPELLING) == base_key
    assert messages_annotation_key(GUARDED_SPELLING) == base_key


def test_resolve_reducer_never_falls_back_for_the_delta_form() -> None:
    # The real base field: what the working-copy machinery resolves.
    assert resolve_reducer(BaseAgentState.model_fields["messages"]) is guarded_add_messages


def test_resolve_reducer_ignores_foreign_delta_channels() -> None:

    def keep(_state: list[AnyMessage], writes: Sequence[list[AnyMessage]]) -> list[AnyMessage]:
        return [message for write in writes for message in write]

    class _Foreign(BaseModel):
        value: Annotated[list[AnyMessage], DeltaChannel(keep, snapshot_frequency=5)] = Field(
            default_factory=list
        )

    assert resolve_reducer(_Foreign.model_fields["value"]) is not guarded_add_messages


def test_accumulate_delta_concatenates_for_every_messages_spelling() -> None:
    acc = [HumanMessage(content="a", id="a")]
    new = [HumanMessage(content="b", id="b")]
    # The delta form is a sanctioned spelling (see is_messages_reducer_form);
    # the parameter's Callable type cannot express that runtime contract.
    for reducer in (add_messages, guarded_add_messages, MESSAGES_DELTA_CHANNEL):
        merged = accumulate_delta(acc, new, reducer)  # pyright: ignore[reportArgumentType]
        assert is_object_list(merged)
        assert [m.id for m in merged if isinstance(m, BaseMessage)] == ["a", "b"]


def test_is_messages_reducer_form_membership() -> None:
    def _noop(_old: object, new: object) -> object:
        return new

    assert is_messages_reducer_form(add_messages)
    assert is_messages_reducer_form(guarded_add_messages)
    assert is_messages_reducer_form(MESSAGES_DELTA_CHANNEL)
    assert not is_messages_reducer_form(_noop)


class _FlippedState(BaseAgentState):
    """Minimal graph state over the real flipped base (fields inherited)."""

    n: int = 0
    target: int = 0


class _FlippedInput(TypedDict):
    n: int
    target: int


type _CompiledGraph = AsyncGraph[_FlippedInput]


# meiva:note
# The shared builder view plus the one extra call this test makes, a conditional edge.
class _GraphBuilder(NodeGraphBuilder[Runnable[_FlippedState, object], _CompiledGraph], Protocol):
    def add_conditional_edges(
        self, source: str, path: Callable[[_FlippedState], str]
    ) -> object: ...


def _new_builder() -> _GraphBuilder:
    return StateGraph(_FlippedState, input_schema=_FlippedInput)


def _build(saver: BaseCheckpointSaver[str]) -> _CompiledGraph:
    def step(state: _FlippedState) -> dict[str, object]:
        n = state.n
        return {
            "messages": [
                HumanMessage(content=f"u{n}", id=f"u{n}"),
                AIMessage(content=f"a{n}", id=f"a{n}"),
            ],
            "n": n + 1,
        }

    def route(state: _FlippedState) -> str:
        return "step" if state.n < state.target else END

    graph = _new_builder()
    graph.add_node("step", RunnableLambda(step))
    graph.add_edge(START, "step")
    graph.add_conditional_edges("step", route)
    return graph.compile(checkpointer=saver)


def _message_ids(state: Mapping[str, object]) -> list[str | None]:
    messages = state["messages"]
    assert is_object_list(messages)
    return [m.id for m in messages if isinstance(m, BaseMessage)]


async def test_flipped_channel_writes_delta_and_resumes_folded() -> None:
    from agent.startup import delta_evidence

    saver = InMemorySaver()
    graph = _build(saver)
    cfg: RunnableConfig = {"configurable": {"thread_id": "switch-rt"}}
    final = object_mapping(await graph.ainvoke({"n": 0, "target": 6}, cfg))
    assert final["n"] == 6
    ids = _message_ids(final)
    assert ids == [f"{p}{n}" for n in range(6) for p in ("u", "a")]

    tup = await saver.aget_tuple(cfg)
    assert tup is not None
    assert "messages" in delta_evidence(tup.checkpoint, tup.metadata)

    final2 = object_mapping(await graph.ainvoke({"n": 6, "target": 6}, cfg))
    ids2 = _message_ids(final2)
    assert ids2[: len(ids)] == ids
    assert len(ids2) == len(ids) + 2
