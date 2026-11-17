from __future__ import annotations

import base64
import json

import pytest
from pydantic import ValidationError

from base.api_contracts.agent_contract import Harness, reads_timeline_from_record
from base.api_contracts.reading import (
    NativeReadingLocator,
    RecordReadingLocator,
    decode_reading_locator,
    encode_reading_locator,
)


def test_record_round_trip_has_source_coordinates_without_a_path() -> None:
    locator = RecordReadingLocator(
        agent_id=42, harness="codex", session_id="session-1", offset=1024, block=2
    )
    assert decode_reading_locator(encode_reading_locator(locator)) == locator
    assert locator.item_id() == "h1024.2"


@pytest.mark.parametrize(
    "identity",
    [
        {"message_id": "message-1"},
        {"inbound_id": 11},
        {"message_id": "message-1", "inbound_id": 11},
    ],
)
def test_native_round_trip_requires_a_stable_identity(identity: dict[str, str | int]) -> None:
    locator = NativeReadingLocator.model_validate(
        {"agent_id": 42, "epoch": "initial", "block": 2, **identity}
    )
    assert decode_reading_locator(encode_reading_locator(locator)) == locator


@pytest.mark.parametrize(
    "changes",
    [
        {"harness": "meiva"},
        {"session_id": "../../other"},
        {"offset": -1},
        {"offset": True},
        {"block": "1"},
        {"agent_id": "42"},
        {"path": "/record"},
        {"message_id": "native-id"},
        {"epoch": "initial"},
        {"version": 2},
    ],
)
def test_record_shape_rejects_mixed_or_invalid_coordinates(changes: dict[str, object]) -> None:
    data = {"agent_id": 42, "harness": "codex", "session_id": "session-1", "offset": 0, "block": 0}
    with pytest.raises(ValidationError):
        _ = RecordReadingLocator.model_validate({**data, **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"message_id": None},
        {"epoch": ""},
        {"message_id": "\x00"},
        {"checkpoint_id": "../other"},
        {"inbound_id": True},
        {"block": -1},
        {"harness": "codex"},
        {"session_id": "session-1"},
        {"item_id": "2.0"},
    ],
)
def test_native_shape_rejects_missing_identity_or_record_fields(changes: dict[str, object]) -> None:
    data = {"agent_id": 42, "epoch": "initial", "message_id": "message-1", "block": 0}
    with pytest.raises(ValidationError):
        _ = NativeReadingLocator.model_validate({**data, **changes})


@pytest.mark.parametrize("value", ["", "!", "a" * 4097, "\u6d88\u606f", "e30", "bm90LWpzb24"])
def test_invalid_locator_text_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        _ = decode_reading_locator(value)


def test_decoder_rejects_unknown_source_and_extra_fields() -> None:
    for source in ["unknown", "native", "record"]:
        raw = json.dumps({"source": source, "agent_id": 42, "extra": True}).encode()
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        with pytest.raises(ValueError):
            _ = decode_reading_locator(encoded)


def test_record_harness_choices_follow_the_record_owner() -> None:
    from typing import get_args

    supported = {value.value for value in Harness if reads_timeline_from_record(value.value)}
    assert set(get_args(RecordReadingLocator.model_fields["harness"].annotation)) == supported
