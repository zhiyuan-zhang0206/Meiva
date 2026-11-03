"""Qualify checkpoint message identities without creating another ID owner."""

from collections.abc import Sequence
from typing import Protocol, TypeGuard, overload

from langchain_core.messages import (
    BaseMessage,
    MessageLikeRepresentation,
    RemoveMessage,
    convert_to_messages,
)
from langchain_core.messages.utils import message_chunk_to_message
from langgraph._internal._typing import MISSING
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.types import Overwrite

from base.agents.messages.kwargs import read_meiva_kwargs
from base.host.boundary.json_values import is_object_list
from base.host.boundary.untyped import object_mapping


# meiva:note
# What `convert_to_messages` converts: a sequence of message-likes. It is the validator of each item
# (it raises for one it cannot read); this only keeps a string or bytes from passing as a sequence.
def is_message_likes(value: object) -> TypeGuard[Sequence[MessageLikeRepresentation]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


class _Held(Protocol):
    @property
    def value(self) -> object: ...


# meiva:note
# The value a `_DeltaSnapshot` or an `Overwrite` wraps, read as `object`: both declare the field
# `Any`. The parameter annotation does the widening.
def held_value(holder: _Held, /) -> object:
    return holder.value


# meiva:note
# A write value as a group of message-likes: a list is the group, anything else a group of one.
# `convert_to_messages` judges every item; this only keeps a string or bytes from passing as the
# sequence.
def message_group(value: object) -> Sequence[MessageLikeRepresentation]:
    group = value if is_object_list(value) else [value]
    if not is_message_likes(group):
        raise TypeError(f"Expected a sequence of messages, got {type(value).__name__}")
    return group


# meiva:note
# One group of message-likes as messages. A message with no id is marked as having none (see
# `normalize_stored_message_ids`).
def normalize_message_group(group: Sequence[MessageLikeRepresentation]) -> list[BaseMessage]:
    messages = [message_chunk_to_message(m) for m in convert_to_messages(group)]
    for message in messages:
        if message.id is None and not isinstance(message, RemoveMessage):
            read_meiva_kwargs(message)["meiva_ephemeral_message_id"] = True
    return messages


# meiva:note
# Mark IDs missing in stored data before a reducer can synthesize a UUID.
#
# This is a read/replay boundary, never a working-copy/new-delta boundary. The reserved metadata
# survives later checkpoint writes: persisting a reconstructed UUID must not upgrade its original
# source identity. Existing IDs, including existing markers, are preserved. LangGraph remains the
# sole ID generator; this function only records provenance.
#
# A list (or one message-like) comes back as a list of messages; the absent value (`MISSING`,
# None), a `_DeltaSnapshot` and an `Overwrite` come back as themselves with their wrapped value
# normalized.
@overload
def normalize_stored_message_ids[T](value: list[T]) -> list[BaseMessage]: ...
@overload
def normalize_stored_message_ids(value: object) -> object: ...
def normalize_stored_message_ids(value: object) -> object:
    if value is MISSING or value is None:
        return value
    if isinstance(value, _DeltaSnapshot):
        return _DeltaSnapshot(normalize_stored_message_ids(held_value(value)))
    if isinstance(value, Overwrite):
        return Overwrite(normalize_stored_message_ids(held_value(value)))
    return normalize_message_group(message_group(value))


def _normalized_writes(
    writes: Sequence[tuple[str, str, object]],
) -> list[tuple[str, str, object]]:
    return [
        (task, channel, normalize_stored_message_ids(value) if channel == "messages" else value)
        for task, channel, value in writes
    ]


def normalize_checkpoint_message_ids(tuple_: CheckpointTuple) -> None:
    """Normalize stored seed and pending message writes at the shared read door."""
    values = tuple_.checkpoint["channel_values"]
    if "messages" in values:
        values["messages"] = normalize_stored_message_ids(object_mapping(values)["messages"])
    if tuple_.pending_writes is not None:
        tuple_.pending_writes[:] = _normalized_writes(tuple_.pending_writes)
