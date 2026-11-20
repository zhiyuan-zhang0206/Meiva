# meiva:note
# A node's `Command.update` read as the dict it is. LangGraph types the field `Any`, and a node
# update maps a state key to a value of that key's own type, so a test narrows the value it asserts
# on instead of holding the `Any`.

from langchain_core.messages import BaseMessage
from langgraph.types import Command

from base.host.boundary.json_values import is_object_dict, is_object_list

__all__ = ["command_update", "update_messages"]


def command_update[Node](cmd: Command[Node]) -> dict[str, object]:
    update = cmd.update
    assert is_object_dict(update)
    return update


# meiva:note
# The `messages` entry of a node update as messages: `add_messages` takes whatever the node wrote,
# so each element is checked rather than assumed.
def update_messages(update: dict[str, object]) -> list[BaseMessage]:
    written = update["messages"]
    assert is_object_list(written)
    messages = [message for message in written if isinstance(message, BaseMessage)]
    assert len(messages) == len(written)
    return messages
