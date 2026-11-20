from collections.abc import Awaitable, Mapping
from types import SimpleNamespace
from typing import Protocol

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from agent.execution.child import import_runtime
from agent.state import BaseAgentState
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


# meiva:note
# `meiva.state.<plugin>`: the namespace view the state class builds for a plugin name, through the
# `__getattr__` that `build_agent_state` puts on the class it returns.
def plugin_namespace_object(plugin: str) -> SimpleNamespace:
    state = sdk.state
    view = object_mapping(type(state).__dict__)["__getattr__"]
    assert callable(view)
    namespace = object_of(view, state, plugin)
    assert isinstance(namespace, SimpleNamespace)
    return namespace


def plugin_namespace(plugin: str) -> Mapping[str, object]:
    return object_mapping(vars(plugin_namespace_object(plugin)))


class _Graph(Protocol):
    async def ainvoke(
        self, input: BaseAgentState, config: RunnableConfig
    ) -> Mapping[str, object]: ...


class _NodeFunction(Protocol):
    def __call__(self, state: BaseAgentState) -> Awaitable[object]: ...


class GraphBuilder(Protocol):
    def add_node(self, node: str, action: _NodeFunction) -> object: ...

    def add_edge(self, start_key: str, end_key: str) -> object: ...

    def compile(self, checkpointer: InMemorySaver) -> _Graph: ...


# meiva:note
# langgraph's `StateGraph` stubs leave part of `add_node`, `compile` and `ainvoke` unknown; this
# view names the three calls the test makes with the types it passes.
def graph_builder(builder: GraphBuilder) -> GraphBuilder:
    return builder
