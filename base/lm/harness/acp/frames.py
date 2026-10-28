from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

type Json = str | int | float | bool | dict[str, Any] | list[Json] | None
JsonObject = dict[str, Any]
Params = Mapping[str, Any]

JSONRPC = "2.0"

# meiva:note
# A JSON-RPC id is a number, a string or null.
type RequestId = int | str | None


def request_frame(request_id: int, method: str, params: Params | None) -> JsonObject:
    frame: JsonObject = {"jsonrpc": JSONRPC, "id": request_id, "method": method}
    if params is not None:
        frame["params"] = dict(params)
    return frame


def notification_frame(method: str, params: Params | None) -> JsonObject:
    frame: JsonObject = {"jsonrpc": JSONRPC, "method": method}
    if params is not None:
        frame["params"] = dict(params)
    return frame


def encode_frame(frame: Params) -> bytes:
    return (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")


# meiva:note
# One line of the child's stdout, decoded. A blank line carries nothing; a line that is not JSON is
# kept as text for the stderr tail; JSON that is not an object is dropped.
@dataclass(frozen=True, slots=True)
class Blank: ...


@dataclass(frozen=True, slots=True)
class NonJson:
    text: str


@dataclass(frozen=True, slots=True)
class NotAnObject: ...


@dataclass(frozen=True, slots=True)
class Frame:
    value: JsonObject


# meiva:note
# What the stderr tail keeps: a stdout line that was not JSON, cut to 300 characters and labelled,
# and each stderr line decoded with replacement and stripped of its line end.
def non_json_note(text: str) -> str:
    return f"non-JSON stdout: {text[:300]}"


def stderr_text(line: bytes) -> str:
    return line.decode("utf-8", "replace").rstrip()


def decode_line(line: bytes) -> Blank | NonJson | NotAnObject | Frame:
    text = line.decode("utf-8", "replace").strip()
    if not text:
        return Blank()
    try:
        frame: Json = json.loads(text)
    except json.JSONDecodeError:
        return NonJson(text)
    return Frame(frame) if isinstance(frame, dict) else NotAnObject()
