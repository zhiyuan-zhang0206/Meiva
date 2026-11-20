# meiva:note
# Typed readers for what a hook returns, and a linear LangGraph run over the real agent state
# schema. langgraph's builder and compiled graph carry `Unknown` generics in their own signatures,
# so the run reaches them through the narrow protocols below: the nodes it adds and the one
# `ainvoke` it calls are checked, and no unchecked value is bound.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START
from langgraph.types import Overwrite

from agent.state import AgentState, SkillRecallState
from base.agents.tests.langgraph_views import NodeGraphBuilder
from base.host.boundary.json_values import is_object_list
from base.host.boundary.untyped import object_of


class ChainNode(Protocol):
    async def __call__(self, state: AgentState) -> dict[str, object] | None: ...


class Compiled(Protocol):
    async def ainvoke(self, input: AgentState, /) -> object: ...


type ChainBuilder = NodeGraphBuilder[ChainNode, Compiled]


async def run_chain(
    builder: ChainBuilder, steps: Sequence[tuple[str, ChainNode]], initial: AgentState
) -> object:
    previous = START
    for name, node in steps:
        builder.add_node(name, node)
        builder.add_edge(previous, name)
        previous = name
    builder.add_edge(previous, END)
    return await builder.compile().ainvoke(initial)


def notes(update: Mapping[str, object] | None) -> list[HumanMessage]:
    assert update is not None
    messages = update["messages"]
    assert is_object_list(messages)
    found = [message for message in messages if isinstance(message, HumanMessage)]
    assert len(found) == len(messages)
    return found


def note_text(update: Mapping[str, object] | None) -> str:
    (note,) = notes(update)
    assert isinstance(note.content, str)
    return note.content


def overwritten(update: Mapping[str, object], key: str) -> object:
    value = update[key]
    assert isinstance(value, Overwrite)
    return object_of(getattr, value, "value")


def recall_record(update: Mapping[str, object]) -> SkillRecallState:
    record = update["skill_recall"]
    assert isinstance(record, SkillRecallState)
    return record
