# meiva:note
# The checkpoint type vocabulary: what a reader loads, what was retired, and how a blob's type
# names are read without building anything.

from __future__ import annotations

import logging

import ormsgpack
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.serde.jsonplus import (
    EXT_DELTA_SNAPSHOT,
    EXT_PYDANTIC_V2,
    _msgpack_enc,
)

from base.agents.history.checkpoint_serde import (
    READER_CHECKPOINT_MSGPACK_TYPES,
    RETIRED_CHECKPOINT_MSGPACK_TYPES,
    STATIC_CHECKPOINT_MSGPACK_TYPES,
    checkpoint_type_status,
    is_retired_checkpoint_value,
    observed_checkpoint_types,
    retired_type,
)
from base.agents.history.checkpoint_serializer import CheckpointSerializer


def _envelope(module: str, name: str) -> ormsgpack.Ext:
    # meiva:note
    # One pydantic-v2 ext envelope, exactly as the serializer writes it.
    return ormsgpack.Ext(
        EXT_PYDANTIC_V2, ormsgpack.packb((module, name, {"value": 1}, "model_validate_json"))
    )


def test_a_blob_names_its_class_without_the_class_existing() -> None:
    # meiva:note
    # The observer reports the name and never imports it: a class this build deleted is exactly the
    # case the census has to see, and importing would raise instead.
    blob = ormsgpack.packb(_envelope("no.such.module", "GhostType"))

    assert observed_checkpoint_types(blob) == {("no.such.module", "GhostType")}


def test_a_name_inside_text_is_not_a_stored_class() -> None:
    # meiva:note
    # Byte matching reads prose as an object; decoding reads only envelopes. The 2026-10-30
    # measurement that reported 20 dead-type hits in `messages` was this mistake — the decoded count
    # was zero.
    blob = ormsgpack.packb({"content": "no.such.module GhostType was here"})

    assert observed_checkpoint_types(blob) == frozenset()


def test_nested_envelopes_are_read_where_the_reader_reaches_them() -> None:
    # meiva:note
    # A class inside a container value, and inside a delta snapshot (which the store writes around a
    # channel's value), is part of what the reader decodes.
    inner = _envelope("deep.module", "Inner")
    in_list = ormsgpack.packb({"values": [inner]})
    in_snapshot = ormsgpack.packb(ormsgpack.Ext(EXT_DELTA_SNAPSHOT, ormsgpack.packb([inner])))

    assert observed_checkpoint_types(in_list) == {("deep.module", "Inner")}
    assert observed_checkpoint_types(in_snapshot) == {("deep.module", "Inner")}


def test_the_writer_names_a_message_class_the_reader_already_loads() -> None:
    # meiva:note
    # The common case: a real message encodes as an envelope whose class is in LangGraph's own safe
    # set, so the census must call it allowed, not unknown.
    blob = _msgpack_enc([HumanMessage(content="hello")])

    named = observed_checkpoint_types(blob)

    assert named == {("langchain_core.messages.human", "HumanMessage")}
    assert {checkpoint_type_status(key) for key in named} == {"allowed"}


def test_only_a_retired_root_envelope_is_skippable() -> None:
    for entry in RETIRED_CHECKPOINT_MSGPACK_TYPES:
        envelope = _envelope(entry.module, entry.name)
        assert is_retired_checkpoint_value(ormsgpack.packb(envelope))
        assert not is_retired_checkpoint_value(ormsgpack.packb({"nested": envelope}))
        assert not is_retired_checkpoint_value(ormsgpack.packb(f"{entry.module}.{entry.name}"))
        assert not is_retired_checkpoint_value(ormsgpack.packb(_envelope(entry.module, "Unknown")))


def test_a_type_is_loadable_retired_or_unaccounted_for() -> None:
    assert checkpoint_type_status(("agent.state_channels", "AttachState")) == "allowed"
    assert checkpoint_type_status(
        ("base.agents.incarnation.native_work_models", "NativeWorkTarget")
    ) == ("retired")
    assert checkpoint_type_status(("nowhere.at.all", "Nothing")) == "unknown"


def test_the_reader_set_covers_the_safe_types_an_allowlist_alone_would_miss() -> None:
    # meiva:note
    # The static allowlist alone is not what the reader loads: messages come from LangGraph's built-
    # in safe set, so a census that compared against the allowlist alone would red every stored
    # conversation.
    assert ("langchain_core.messages.ai", "AIMessage") in READER_CHECKPOINT_MSGPACK_TYPES
    assert ("agent.state_channels", "AttachState") in READER_CHECKPOINT_MSGPACK_TYPES
    assert ("langchain_core.messages.ai", "AIMessage") not in STATIC_CHECKPOINT_MSGPACK_TYPES


def test_a_retired_type_is_never_also_loadable() -> None:
    # meiva:note
    # Registering a retired class in the allowlist would bring it back and silence the only signal
    # the census has for it.
    for entry in RETIRED_CHECKPOINT_MSGPACK_TYPES:
        assert entry.key not in READER_CHECKPOINT_MSGPACK_TYPES
        assert entry.key not in STATIC_CHECKPOINT_MSGPACK_TYPES
        assert retired_type(entry.key) is entry
    assert retired_type(("nowhere.at.all", "Nothing")) is None


def test_every_retirement_carries_the_commit_and_the_time_it_landed() -> None:
    for entry in RETIRED_CHECKPOINT_MSGPACK_TYPES:
        assert len(entry.retired_in) >= 10
        assert entry.retired_at.tzinfo is not None
        assert entry.note


def test_checkpoint_serializer_skips_retired_root_envelopes_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import langgraph.checkpoint.serde.jsonplus as _jsonplus

    _jsonplus._warned_blocked_types.clear()
    serde = CheckpointSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES)
    serde_logger = "langgraph.checkpoint.serde.jsonplus"

    for entry in RETIRED_CHECKPOINT_MSGPACK_TYPES:
        blob = ormsgpack.packb(_envelope(entry.module, entry.name))
        with caplog.at_level(logging.WARNING, logger=serde_logger):
            assert serde.loads_typed(("msgpack", blob)) is None
        warnings = [
            r.message
            for r in caplog.get_records("call")
            if r.name == serde_logger and r.levelno >= logging.WARNING
        ]
        assert warnings == []

    # meiva:note
    # Negative controls: unregistered unknown types and nested retired envelopes still go
    # through the allowlist decoder and emit the blocked-deserialization warning.
    unknown_blob = ormsgpack.packb(_envelope("unknown.module", "UnknownState"))
    nested_blob = ormsgpack.packb(
        {
            "nested": _envelope(
                "base.agents.incarnation.native_work_models",
                "NativeWorkTarget",
            )
        }
    )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=serde_logger):
        assert serde.loads_typed(("msgpack", unknown_blob)) == {"value": 1}
        assert serde.loads_typed(("msgpack", nested_blob)) == {"nested": {"value": 1}}
    blocked_warnings = [
        r.message
        for r in caplog.get_records("call")
        if r.name == serde_logger and r.levelno >= logging.WARNING
    ]
    assert any(
        "Blocked deserialization of unknown.module.UnknownState" in m for m in blocked_warnings
    )
    assert any("NativeWorkTarget" in m and "Blocked deserialization" in m for m in blocked_warnings)
