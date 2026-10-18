"""The K1 control-plane wire protocol (one JSON object per line).

The root supervisor speaks this protocol over a Unix socket. The two ends of this module are transport-only:
`services.supervision.meiva_root.server` binds the socket, `base.native_process.root_control.client` dials
it, and both validate shapes fail-fast so a drifting peer is rejected at the
boundary instead of being carried into the supervisor.

Wire shape — one UTF-8 line per message, newline-terminated:

    request   {"verb": "up", "name": "gateway"}
              {"verb": "status"}
    response  {"ok": true, "result": {...}}
              {"ok": false, "code": "unknown_unit", "error": "unknown unit 'x'"}
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import NotRequired, TypedDict, cast

# One message is one line; the cap bounds how much a peer can make us buffer.
MAX_MESSAGE_BYTES = 64 * 1024

# The verbs that name a target unit; the others take no name.
_NAMED_VERBS = frozenset({"up", "down", "force-down", "restart"})
STATE_VERBS = frozenset({"up", "down", "force-down", "restart", "shutdown"})
_CALLER_FIELDS = frozenset(
    {"pid", "ppid", "uid", "cwd", "argv", "agent_id", "machine", "operation_id", "env_keys"}
)
_REQUEST_FIELDS = frozenset({"verb", "name", "payload", "caller"})
_RESPONSE_FIELDS = frozenset({"ok", "result", "error", "code"})


class Verb(StrEnum):
    """The K1 control verbs."""

    UP = "up"
    DOWN = "down"
    FORCE_DOWN = "force-down"
    RESTART = "restart"
    STATUS = "status"
    SHUTDOWN = "shutdown"
    RESOURCE = "resource"


class UnitState(StrEnum):
    """What a unit's process is doing right now."""

    RUNNING = "running"
    STOPPED = "stopped"


class ErrorCode(StrEnum):
    """Stable failure codes; clients branch on these, not on message text."""

    INVALID_REQUEST = "invalid_request"
    UNKNOWN_VERB = "unknown_verb"
    UNKNOWN_UNIT = "unknown_unit"
    INTERNAL = "internal"


# meiva:note
# Who asked for a state change, in wire form. Values never leave the process:
# `env_keys` names variables (never their values), and `argv` arrives already
# redacted and truncated by the client.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
class CallerPayload(TypedDict):
    pid: int
    ppid: int
    uid: int
    cwd: str
    argv: list[str]
    agent_id: str | None
    machine: str
    operation_id: str | None
    env_keys: list[str]


class RequestPayload(TypedDict):
    """A validated request in wire form."""

    verb: str
    name: NotRequired[str]
    payload: NotRequired[dict[str, object]]
    caller: NotRequired[CallerPayload]


class ResponsePayload(TypedDict):
    """A response in wire form; either `result` (ok) or `error` + `code`."""

    ok: bool
    result: NotRequired[object]
    error: NotRequired[str]
    code: NotRequired[str]


class ProtocolError(ValueError):
    """A wire message is malformed; the message is rejected, never guessed at."""


class UnknownVerbError(ProtocolError):
    """The message names a verb this protocol does not define."""


def encode(message: Mapping[str, object]) -> bytes:
    """One wire message as a single newline-terminated UTF-8 line."""
    line = json.dumps(message, ensure_ascii=True, separators=(",", ":"))
    return line.encode("utf-8") + b"\n"


def parse_request(raw: bytes) -> RequestPayload:
    """Validate one request line; every deviation raises ProtocolError."""
    document = _decode_object(raw)
    unknown = set(document) - _REQUEST_FIELDS
    if unknown:
        raise ProtocolError(f"unknown request field(s): {sorted(unknown)}")
    verb_raw = document.get("verb")
    if not isinstance(verb_raw, str):
        raise ProtocolError("request must carry a string 'verb'")
    name_raw = document.get("name")
    caller = _parse_caller(document.get("caller"), verb_raw)
    if verb_raw == Verb.RESOURCE:
        request = _resource_request(name_raw, document.get("payload"))
    else:
        request = _verb_request(verb_raw, name_raw, document)
    if caller is not None:
        request["caller"] = caller
    return request


def _resource_request(name_raw: object, payload: object) -> RequestPayload:
    if not isinstance(name_raw, str) or not name_raw:
        raise ProtocolError("resource requires a non-empty 'name'")
    if not isinstance(payload, dict):
        raise ProtocolError("resource requires an object 'payload'")
    return {"verb": Verb.RESOURCE, "name": name_raw, "payload": payload}


def _verb_request(
    verb_raw: str, name_raw: object, document: Mapping[str, object]
) -> RequestPayload:
    if "payload" in document:
        raise ProtocolError(f"verb {verb_raw!r} takes no 'payload'")
    if verb_raw in _NAMED_VERBS:
        if not isinstance(name_raw, str) or not name_raw:
            raise ProtocolError(f"verb {verb_raw!r} requires a non-empty 'name'")
        return {"verb": verb_raw, "name": name_raw}
    try:
        Verb(verb_raw)
    except ValueError as exc:
        raise UnknownVerbError(f"unknown verb {verb_raw!r}") from exc
    if "name" in document:
        raise ProtocolError(f"verb {verb_raw!r} takes no 'name'")
    return {"verb": verb_raw}


# meiva:note
# Validate the optional caller block fail-fast; a verb that changes nothing
# rejects it rather than dropping it silently.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md
def _parse_caller(raw: object, verb: str) -> CallerPayload | None:
    if raw is None:
        return None
    if verb not in STATE_VERBS:
        raise ProtocolError(f"verb {verb!r} takes no 'caller'")
    if not isinstance(raw, dict):
        raise ProtocolError("'caller' must be an object")
    values = cast("dict[str, object]", raw)
    unknown = set(values) - _CALLER_FIELDS
    if unknown:
        raise ProtocolError(f"unknown caller field(s): {sorted(unknown)}")
    missing = _CALLER_FIELDS - set(values)
    if missing:
        raise ProtocolError(f"caller is missing field(s): {sorted(missing)}")
    caller: CallerPayload = {
        "pid": _positive_int(values["pid"], "pid"),
        "ppid": _positive_int(values["ppid"], "ppid"),
        "uid": _positive_int(values["uid"], "uid"),
        "cwd": _text(values["cwd"], "cwd"),
        "argv": _texts(values["argv"], "argv"),
        "agent_id": _optional_text(values["agent_id"], "agent_id"),
        "machine": _text(values["machine"], "machine"),
        "operation_id": _optional_text(values["operation_id"], "operation_id"),
        "env_keys": _texts(values["env_keys"], "env_keys"),
    }
    return caller


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(f"caller {field!r} must be a non-negative integer")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"caller {field!r} must be a string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _texts(value: object, field: str) -> list[str]:
    items = cast("list[object]", value) if isinstance(value, list) else None
    if items is None or not all(isinstance(item, str) for item in items):
        raise ProtocolError(f"caller {field!r} must be a list of strings")
    return cast("list[str]", items)


def parse_response(raw: bytes) -> ResponsePayload:
    """Validate one response line; every deviation raises ProtocolError."""
    document = _decode_object(raw)
    unknown = set(document) - _RESPONSE_FIELDS
    if unknown:
        raise ProtocolError(f"unknown response field(s): {sorted(unknown)}")
    ok_raw = document.get("ok")
    if not isinstance(ok_raw, bool):
        raise ProtocolError("response must carry a boolean 'ok'")
    if ok_raw:
        accepted: ResponsePayload = {"ok": True, "result": document.get("result")}
        return accepted
    error_raw = document.get("error")
    if not isinstance(error_raw, str):
        raise ProtocolError("a failed response must carry a string 'error'")
    code_raw = document.get("code")
    try:
        code = ErrorCode(code_raw)
    except ValueError as exc:
        raise ProtocolError(f"failed response carries an unknown code {code_raw!r}") from exc
    rejected: ResponsePayload = {"ok": False, "error": error_raw, "code": code.value}
    return rejected


def parse_status_response(raw: bytes) -> ResponsePayload:
    """Validate every unit state before a successful status can be consumed."""
    response = parse_response(raw)
    if not response["ok"]:
        return response
    body = response.get("result")
    if not isinstance(body, dict):
        raise ProtocolError("root status omitted its unit roster")
    rows = cast("dict[str, object]", body).get("units")
    if not isinstance(rows, list):
        raise ProtocolError("root status omitted its unit roster")
    for item in cast("list[object]", rows):
        if not isinstance(item, dict):
            raise ProtocolError("root status contains an invalid unit")
        row = cast("dict[str, object]", item)
        if not isinstance(row.get("id"), str) or not row["id"]:
            raise ProtocolError("root status contains an invalid unit id")
        state = row.get("state")
        if not isinstance(state, str):
            raise ProtocolError("root status must carry a string unit state")
        try:
            UnitState(state)
        except ValueError as exc:
            raise ProtocolError(f"root status carries an unknown unit state {state!r}") from exc
    return response


def ok_response(result: object) -> ResponsePayload:
    """A success response carrying `result`."""
    return {"ok": True, "result": result}


def error_response(code: ErrorCode, message: str) -> ResponsePayload:
    """A failure response carrying a stable code and a human message."""
    return {"ok": False, "code": code.value, "error": message}


def _decode_object(raw: bytes) -> dict[str, object]:
    """Decode one message into a JSON object; anything else raises ProtocolError."""
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"message is not UTF-8: {exc}") from exc
    try:
        value = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"message is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("message must be a JSON object")
    return cast("dict[str, object]", value)
