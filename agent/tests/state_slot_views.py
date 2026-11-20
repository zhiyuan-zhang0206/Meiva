from collections.abc import Awaitable, Mapping
from types import SimpleNamespace
from typing import Protocol, TypedDict

from langchain_core.messages import HumanMessage

from agent.execution.child import import_runtime
from agent.state import BaseAgentState
from base.agents.tests.langgraph_views import AsyncGraph, NodeGraphBuilder
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_mapping, object_of

# meiva:note
# The `meiva` module as the exec child reads it: the module declares `state: Any`, this view `object`.
sdk = import_runtime(0.0).meiva


# meiva:note
# What `meiva.state_update` holds this turn, narrowed from `object` to a dict.
def state_update() -> dict[str, object]:
    value = sdk.state_update
    assert is_object_dict(value)
    return value


def _namespace(value: object) -> SimpleNamespace:
    assert isinstance(value, SimpleNamespace)
    return value


# meiva:note
# `meiva.state.meiva_code`, `meiva.state.plugin` and `meiva.state.nonexistent`: the namespace views
# the state class builds, read through the `__getattr__` that `build_agent_state` puts on it. One
# reader per plugin name the tests use, so every attribute name is a literal.
def meiva_code_namespace() -> Mapping[str, object]:
    return object_mapping(vars(_namespace(object_of(getattr, sdk.state, "meiva_code"))))


def plugin_namespace_object() -> SimpleNamespace:
    return _namespace(object_of(getattr, sdk.state, "plugin"))


def plugin_namespace() -> Mapping[str, object]:
    return object_mapping(vars(plugin_namespace_object()))


def nonexistent_namespace() -> SimpleNamespace:
    return _namespace(object_of(getattr, sdk.state, "nonexistent"))


class GraphInput(TypedDict):
    messages: list[HumanMessage]


class _NodeFunction(Protocol):
    def __call__(self, state: BaseAgentState) -> Awaitable[object]: ...


type GraphBuilder = NodeGraphBuilder[_NodeFunction, AsyncGraph[GraphInput]]


# meiva:note
# langgraph's `StateGraph` stubs leave part of `add_node`, `compile` and `ainvoke` unknown; this
# view names the three calls the test makes with the types it passes.
def graph_builder(builder: GraphBuilder) -> GraphBuilder:
    return builder
