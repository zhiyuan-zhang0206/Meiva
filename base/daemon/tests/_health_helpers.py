from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Mapping

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.host.env import port_lease


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def find_free_port() -> int:
    return port_lease.lease_ports(1)[0]


async def http_get(port: int, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    status = int(status_line.split(" ")[1])
    return status, body


def probe_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/healthz"


# meiva:note
# A response body that must be one JSON object.
def json_object(body: bytes) -> dict[str, object]:
    payload = object_of(json.loads, body)
    assert is_object_dict(payload), f"expected a JSON object, got {payload!r}"
    return payload


# meiva:note
# The JSON object under `key` of a decoded response.
def child_object(parent: Mapping[str, object], key: str) -> dict[str, object]:
    child = parent[key]
    assert is_object_dict(child), f"{key!r} is not a JSON object: {child!r}"
    return child


# meiva:note
# The port a bound socket listens on; `getsockname` is typed `Any` in the standard library.
def bound_port(sock: socket.socket) -> int:
    address = object_of(sock.getsockname)
    assert isinstance(address, tuple) and isinstance(address[1], int)
    return address[1]
