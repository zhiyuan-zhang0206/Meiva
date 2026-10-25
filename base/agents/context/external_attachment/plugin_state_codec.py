from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any, TypeGuard

from langchain_core.messages import RemoveMessage, convert_to_messages
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel

from base.agents.context.external_attachment import state_schema as agent_state
from base.agents.context.external_attachment.state_schema import plugin_state_schema
from base.agents.history.delta_read_compat import reconstruct_delta_messages
from base.db import Database
from base.packages.plugins.loading import registry as extensions_registry


def _is_str_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


def _as_dict_row_conn(value: object) -> TypeGuard[Connection[DictRow]]:
    return isinstance(value, Connection)


# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md
def serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        allowed_msgpack_modules=agent_state.checkpoint_msgpack_allowlist(
            extensions_registry.loaded_state_classes()
        )
    )


def _validate_messages_delta(delta: Mapping[str, Any]) -> None:
    if "messages" not in delta:
        return
    value = delta["messages"]
    messages = value if _is_object_list(value) else [value]
    for message in convert_to_messages(messages):
        if isinstance(message, RemoveMessage) and message.id == REMOVE_ALL_MESSAGES:
            raise ValueError(
                "external plugin deltas cannot use REMOVE_ALL to reset message history; "
                "full-history resets are reserved for native compaction and crash repair"
            )


# meiva:note
# Serializes a reducer input validated against `state_cls` without coercing it to a
# field value, preserving partial mappings, sets, nested models and message objects.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md
def encode_plugin_delta(delta: Mapping[str, Any], state_cls: type[Any]) -> dict[str, str]:
    checked = agent_state.validate_plugin_state_keys(dict(delta), state_cls)
    _validate_messages_delta(checked)
    encoding, payload = serializer().dumps_typed(checked)
    return {"encoding": encoding, "data": base64.b64encode(payload).decode("ascii")}


# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md
def decode_plugin_delta(encoded: Mapping[str, Any], state_cls: type[Any]) -> dict[str, Any]:
    payload = serializer().loads_typed(
        (encoded["encoding"], base64.b64decode(encoded["data"], validate=True))
    )
    if not _is_str_dict(payload):
        raise TypeError("external plugin delta must decode to a dict")
    checked = agent_state.validate_plugin_state_keys(payload, state_cls)
    _validate_messages_delta(checked)
    return checked


# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md
def apply_plugin_delta(state: BaseModel, delta: Mapping[str, Any]) -> None:
    agent_state.validate_plugin_state_keys(dict(delta), type(state))
    _validate_messages_delta(delta)
    for name, value in delta.items():
        field = type(state).model_fields[name]
        reducer = agent_state.resolve_reducer(field)
        setattr(state, name, reducer(getattr(state, name), value))


# meiva:note
# Reads native state and pinned config using the loaded plugin state classes as the
# checkpoint decode allowlist, reconstructing delta-written messages without writing
# or updating a checkpoint.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md
def load_snapshot(
    agent_id: int, *, database: Database
) -> tuple[Any, dict[str, Any] | None, dict[str, Any] | None]:
    registry = extensions_registry.build_registry(validate_state=plugin_state_schema)
    state_cls = agent_state.build_agent_state(registry)
    with database.connect(autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s", (agent_id,)
            )
            row = cur.fetchone()
        if row is None:
            raise LookupError(f"agent {agent_id} not found")
        assert _as_dict_row_conn(conn)  # noqa: S101
        conn.row_factory = dict_row
        saver = PostgresSaver(conn=conn, serde=serializer())
        snapshot = saver.get_tuple({"configurable": {"thread_id": str(agent_id)}})
        if snapshot is not None:
            reconstruct_delta_messages(saver, snapshot)
    if snapshot is None:
        raise RuntimeError("approved agent has no checkpoint to attach")
    state = state_cls.model_validate(snapshot.checkpoint["channel_values"])
    return state, row["config_overlay"], row["birth_config"]
