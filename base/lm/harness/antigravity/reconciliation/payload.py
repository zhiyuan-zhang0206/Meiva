from __future__ import annotations

import base64
import binascii

from base.lm.harness.antigravity.trajectory import AgyInterfaceError

# meiva:note
# The smallest part of a queued message's `stepPayload` (a base64 protobuf `CortexStep`) needed to
# find our id: the step's field 19 is its user input, a message whose own field 19 is the
# `clientMessageId` string (observed on agy 1.5.5 from the binary's proto descriptors and a recorded
# queue file). Only those two levels are walked, with every field's wire type checked; a payload that
# does not parse, or whose user input is not a message, raises instead of answering "no id", because
# an unreadable file read as "not ours" could cause a re-delivery.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
_USER_INPUT_FIELD = 19
_CLIENT_ID_FIELD = 19


def client_message_id(step_payload_base64: str) -> str | None:
    try:
        data = base64.b64decode(step_payload_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AgyInterfaceError("a queued message's stepPayload is not base64") from exc
    for number, value in _fields(data):
        if number == _USER_INPUT_FIELD and isinstance(value, bytes):
            return _client_id(value)
    return None


def _client_id(user_input: bytes) -> str | None:
    for number, value in _fields(user_input):
        if number == _CLIENT_ID_FIELD and isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AgyInterfaceError("a clientMessageId is not UTF-8") from exc
    return None


def _varint(data: bytes, position: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while position < len(data):
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7
    raise AgyInterfaceError("a protobuf varint is truncated")


def _fields(data: bytes) -> list[tuple[int, int | bytes]]:
    fields: list[tuple[int, int | bytes]] = []
    position = 0
    while position < len(data):
        key, position = _varint(data, position)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, position = _varint(data, position)
            fields.append((number, value))
        elif wire == 2:
            size, position = _varint(data, position)
            if position + size > len(data):
                raise AgyInterfaceError("a protobuf field is truncated")
            fields.append((number, data[position : position + size]))
            position += size
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if position + width > len(data):
                raise AgyInterfaceError("a protobuf field is truncated")
            fields.append((number, data[position : position + width]))
            position += width
        else:
            raise AgyInterfaceError(f"unsupported protobuf wire type {wire}")
    return fields
