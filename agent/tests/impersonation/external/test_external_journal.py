# meiva:note
# What an attachment journals and what it replays. Plugin updates leave the process as one ordered
# delta under the lease's version, the native checkpoint is never written, a receipted entry is
# not applied twice, messages append without replacing native history, and a reset of the whole
# history is refused before any state moves. The delta codec rules are asserted on the state class
# the framework built from the discovered sample plugin.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md

from __future__ import annotations

import base64
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import meiva
from agent import state as state_module
from agent.tests.impersonation.external.runtime import Takeover, borrowed_agent_id
from agent.tests.impersonation.external.sample_plugin import ExampleMessagesPlugin, ExamplePlugin
from base.agents.context.external_attachment import plugin_state_codec
from base.agents.context.external_attachment.plugin_state_codec import (
    apply_plugin_delta,
    decode_plugin_delta,
    encode_plugin_delta,
    load_snapshot,
)
from meiva import impersonation
from meiva.sdk_surface import process_context


def native_messages(takeover: Takeover) -> list[Any]:
    snapshot, _, _ = load_snapshot(takeover.agent_id, database=takeover.cluster.database)
    return snapshot.messages


def test_plugin_updates_journal_once_and_next_attachment_sees_them(
    attached_runtime: Takeover, state_cls: type[Any]
) -> None:
    handle = state_module.PluginStateHandle(ExamplePlugin, "sample")
    with impersonation.attach(attached_runtime.lease_id):
        handle.update({"seen": {"one"}})
        handle.update({"seen": {"two"}})
        assert handle.read().seen == {"native", "one", "two"}
    journal = attached_runtime.journal()
    assert len(journal) == 1
    assert decode_plugin_delta(journal[0], state_cls) == {"sample__seen": {"one", "two"}}
    with impersonation.attach(attached_runtime.lease_id):
        assert handle.read().seen == {"native", "one", "two"}
    assert len(attached_runtime.journal()) == 1


def test_receipted_journal_entries_are_not_replayed(
    attached_runtime: Takeover, state_cls: type[Any]
) -> None:
    attached_runtime.journal_as_another_controller(
        {"messages": [RemoveMessage(id="already-removed")]}
    )
    attached_runtime.write_checkpoint(
        impersonation_applied={"lease_id": attached_runtime.lease_id, "version": 1}
    )
    with impersonation.attach(attached_runtime.lease_id):
        assert meiva.state.messages == []


def test_external_attachment_appends_messages_without_replacing_native_history(
    attached_runtime: Takeover, state_cls: type[Any]
) -> None:
    native_message = HumanMessage(content="Native history", id="native")
    appended_message = HumanMessage(content="External progress", id="external")
    attached_runtime.write_checkpoint(messages=[native_message])
    handle = state_module.PluginStateHandle(ExampleMessagesPlugin, "sample")
    with impersonation.attach(attached_runtime.lease_id):
        handle.update({"messages": [appended_message]})
        assert handle.read().messages == [native_message, appended_message]
    journal = attached_runtime.journal()
    assert len(journal) == 1
    assert decode_plugin_delta(journal[0], state_cls) == {"messages": [appended_message]}
    assert native_messages(attached_runtime) == [native_message]
    with impersonation.attach(attached_runtime.lease_id):
        assert handle.read().messages == [native_message, appended_message]
    assert len(attached_runtime.journal()) == 1


def test_external_attachment_refuses_to_journal_a_full_history_reset(
    attached_runtime: Takeover,
) -> None:
    native_message = HumanMessage(content="Native history", id="native")
    attached_runtime.write_checkpoint(messages=[native_message])
    attachment = impersonation.attach(attached_runtime.lease_id)
    process_context.set_state_update({"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]})
    with pytest.raises(ValueError, match=r"REMOVE_ALL.*native compaction"):
        attachment.close()
    assert attached_runtime.journal() == []
    assert borrowed_agent_id() is None
    assert native_messages(attached_runtime) == [native_message]


def test_delta_codec_preserves_sets_and_message_objects(state_cls: type[Any]) -> None:
    delta = {
        "sample__seen": {"a", "b"},
        "messages": [HumanMessage(content="note", id="note"), RemoveMessage(id="old")],
    }
    decoded = decode_plugin_delta(encode_plugin_delta(delta, state_cls), state_cls)
    assert decoded == delta
    assert isinstance(decoded["sample__seen"], set)
    assert isinstance(decoded["messages"][1], RemoveMessage)


def test_delta_codec_rejects_framework_state_injection(state_cls: type[Any]) -> None:
    with pytest.raises(ValueError, match="framework core"):
        encode_plugin_delta({"halted": False}, state_cls)


@pytest.mark.parametrize("boundary", ["encode", "decode", "apply"])
@pytest.mark.parametrize(
    "messages",
    [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        [RemoveMessage(id=REMOVE_ALL_MESSAGES), HumanMessage(content="replacement")],
        [{"type": "remove", "id": REMOVE_ALL_MESSAGES, "content": ""}],
    ],
    ids=["single-marker", "wipe-and-replace", "message-dict"],
)
def test_external_delta_rejects_full_history_reset_before_any_mutation(
    state_cls: type[Any], boundary: str, messages: Any
) -> None:
    snapshot = state_cls(
        sample__seen={"native"}, messages=[HumanMessage(content="Native history", id="native")]
    )
    before = snapshot.model_copy(deep=True)
    delta = {"sample__seen": {"must-not-apply"}, "messages": messages}
    with pytest.raises(ValueError, match=r"REMOVE_ALL.*native compaction"):
        if boundary == "encode":
            encode_plugin_delta(delta, state_cls)
        elif boundary == "decode":
            encoding, payload = plugin_state_codec.serializer().dumps_typed(delta)
            decode_plugin_delta(
                {"encoding": encoding, "data": base64.b64encode(payload).decode("ascii")},
                state_cls,
            )
        else:
            apply_plugin_delta(snapshot, delta)
    assert snapshot == before
