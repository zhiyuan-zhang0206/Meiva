from __future__ import annotations

from langchain_core.messages import AnyMessage
from langgraph.types import Command
from pydantic import TypeAdapter

from base.agents.tests.langgraph_views import command_update
from base.events.live.projection import EVENT_ADAPTER, Event
from base.events.live.tests.live_capture import LiveEvents
from base.host.boundary.json_values import is_object_dict, is_object_list

_MESSAGES = TypeAdapter(list[AnyMessage])


# meiva:note
# The `messages` of a node's update, validated as the messages the graph state holds.
def messages_of[N](command: Command[N]) -> list[AnyMessage]:
    return _MESSAGES.validate_python(command_update(command)["messages"])


# meiva:note
# The prompt of the first `harness_inputs` entry of a node's update.
def first_prompt[N](command: Command[N]) -> str:
    inputs = command_update(command)["harness_inputs"]
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
