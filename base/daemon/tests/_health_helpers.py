from __future__ import annotations

import asyncio

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
