# meiva:note
# Tests' views of langgraph, declared once. The library types `Command.update` as `Any`, and its
# builder and compiled graph carry overloads with unresolved types, so a test holds each through a
# narrow view of the calls it makes instead of binding an unchecked value.

from collections.abc import Mapping
from typing import Protocol

from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from base.host.boundary.json_values import is_object_dict, is_object_list

__all__ = ["AsyncGraph", "NodeGraphBuilder", "command_update", "update_messages"]


# meiva:note
# A node's `Command.update` read as the dict it is. A node update maps a state key to a value of that
# key's own type, so a test narrows the value it asserts on instead of holding the `Any`.
def command_update[Node](cmd: Command[Node]) -> dict[str, object]:
    update = cmd.update
    assert is_object_dict(update)
    return update


# meiva:note
# The `messages` entry of a node update, or of a checkpoint's state values, as messages:
# `add_messages` takes whatever was written, so each element is checked rather than assumed.
def update_messages(update: Mapping[str, object]) -> list[BaseMessage]:
    written = update["messages"]
    assert is_object_list(written)
    messages = [message for message in written if isinstance(message, BaseMessage)]
    assert len(messages) == len(written)
    return messages


# meiva:note
# A `StateGraph` builder as a test wires it: nodes and plain edges, then `compile`. `Action` is the
# node the test adds and `Compiled` the graph it gets back.
class NodeGraphBuilder[Action, Compiled](Protocol):
    def add_node(self, node: str, action: Action, /) -> object: ...

    def add_edge(self, start_key: str, end_key: str, /) -> object: ...

    def compile(self, checkpointer: BaseCheckpointSaver[str] | None = None) -> Compiled: ...


# meiva:note
# A compiled graph a test runs with `ainvoke`, reading the final state as a mapping.
class AsyncGraph[Input](Protocol):
    async def ainvoke(self, input: Input, config: RunnableConfig, /) -> Mapping[str, object]: ...
