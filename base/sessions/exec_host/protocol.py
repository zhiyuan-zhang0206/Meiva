# meiva:note
# Wire format of the exec-host service: one JSON object per line over `exec-host.sock`.
#
# A request is `{"id": <int>, "method": <str>, ...fields}`.
# - `ping`: answered immediately with `ok(req_id)` so `protocol_probe.ping` certifies readiness.
# - `run`: spawns `agent.execution.child` in its own session and process group, streams
#   `output(req_id, data)` frames as stdout/stderr arrives, accepts an optional in-band
#   `{"id": <int>, "method": "cancel"}` frame on the same connection, and finishes with
#   `ok(req_id, {"returncode": ..., "cancelled": ..., "timed_out": ...})` (or `err(...)` if
#   the child could not be spawned).

from __future__ import annotations

import json
import socket
from typing import Any, cast

MAX_REQUEST_BYTES = 1 << 20

OK = 0
ERROR = 1
BAD_REQUEST = 2


def ok(req_id: object, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": req_id, "ok": True, "code": OK, "data": data, "error": None}


def err(req_id: object, code: int, message: str) -> dict[str, Any]:
    return {"id": req_id, "ok": False, "code": code, "data": None, "error": message}


def output_chunk(req_id: object, data: str) -> dict[str, Any]:
    return {"id": req_id, "type": "output", "data": data}


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message) + "\n").encode("utf-8")


def read_line(conn: socket.socket, limit: int = MAX_REQUEST_BYTES) -> bytes | None:
    buf = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        buf += chunk
        if b"\n" in buf:
            return buf.split(b"\n", 1)[0]
        if len(buf) > limit:
            raise ValueError(f"line exceeds {limit} bytes")


def decode_object(line: bytes) -> dict[str, Any]:
    value = json.loads(line.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError("message is not a JSON object")
    return cast("dict[str, Any]", value)
