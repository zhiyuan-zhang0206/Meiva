from __future__ import annotations

from collections.abc import Mapping

from langchain_core.messages import AnyMessage, BaseMessage
from langgraph.types import Command
from pydantic import TypeAdapter

from base.events.live.projection import EVENT_ADAPTER, Event
from base.events.live.tests.live_capture import LiveEvents
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of

_MESSAGES = TypeAdapter(list[AnyMessage])


# meiva:note
# A node's update, narrowed: LangGraph types `Command.update` as `Any`, and a node's update maps each
# channel name to a value whose type differs by key.
def update_of[N](command: Command[N]) -> Mapping[str, object]:
    update = object_of(lambda: command.update)
    assert is_object_dict(update)
    return update


# meiva:note
# The `messages` of a node's update, validated as the messages the graph state holds.
def messages_of[N](command: Command[N]) -> list[AnyMessage]:
    return _MESSAGES.validate_python(update_of(command)["messages"])


# meiva:note
# The `messages` of a node's update as the reducer receives them: messages, including the removals a
# compaction writes, which the state's message union leaves out.
def reducer_messages_of[N](command: Command[N]) -> list[BaseMessage]:
    written = update_of(command)["messages"]
    assert is_object_list(written)
    messages = [message for message in written if isinstance(message, BaseMessage)]
    assert len(messages) == len(written)
    return messages


# meiva:note
# The prompt of the first `harness_inputs` entry of a node's update.
def first_prompt[N](command: Command[N]) -> str:
    inputs = update_of(command)["harness_inputs"]
    assert is_object_list(inputs)
    first = inputs[0]
    assert is_object_dict(first)
    prompt = first["prompt"]
    assert isinstance(prompt, str)
    return prompt


# meiva:note
# The events published since the previous read, as the live-view event union: `LiveEvents.typed`
# answers `Any`, the adapter it validates with answers `Event`.
async def typed_events(live_events: LiveEvents) -> list[Event]:
    return [EVENT_ADAPTER.validate_json(payload) for payload in await live_events.payloads()]
