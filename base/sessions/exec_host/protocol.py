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
from collections.abc import Mapping

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

MAX_REQUEST_BYTES = 1 << 20

OK = 0
ERROR = 1
BAD_REQUEST = 2


def ok(req_id: object, data: Mapping[str, object] | None = None) -> dict[str, object]:
    return {"id": req_id, "ok": True, "code": OK, "data": data, "error": None}


def err(req_id: object, code: int, message: str) -> dict[str, object]:
    return {"id": req_id, "ok": False, "code": code, "data": None, "error": message}


def output_chunk(req_id: object, data: str) -> dict[str, object]:
    return {"id": req_id, "type": "output", "data": data}


def encode(message: Mapping[str, object]) -> bytes:
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


def decode_object(line: bytes) -> dict[str, object]:
    value = object_of(json.loads, line.decode("utf-8"))
    if not is_object_dict(value):
        raise TypeError("message is not a JSON object")
    return value
