# meiva:note
# Real asyncio streams for the line-protocol tests. A connected socket pair gives the code under test a genuine (reader, writer) and the test the other end, `LinePeer`, which speaks and hears JSON lines.
from __future__ import annotations

import asyncio
import json
import socket
import tempfile
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

LINE_LIMIT = 64 * 1024 * 1024


@dataclass
class LinePeer:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    def send(self, payload: dict[str, Any]) -> None:
        self.writer.write((json.dumps(payload) + "\n").encode())

    def send_raw(self, data: bytes) -> None:
        self.writer.write(data)

    async def receive(self, timeout: float = 5.0) -> dict[str, Any]:
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        return json.loads(line)

    async def receive_eof(self, timeout: float = 5.0) -> bool:
        return await asyncio.wait_for(self.reader.readline(), timeout) == b""

    async def close(self) -> None:
        self.writer.close()
        with suppress(OSError):
            await self.writer.wait_closed()


# meiva:note
# ((reader, writer) for the code under test, the peer at the other end of the socket pair).
@asynccontextmanager
async def line_pair() -> AsyncGenerator[
    tuple[tuple[asyncio.StreamReader, asyncio.StreamWriter], LinePeer]
]:
    near, far = socket.socketpair()
    mine = await asyncio.open_connection(sock=near, limit=LINE_LIMIT)
    peer = LinePeer(*await asyncio.open_connection(sock=far, limit=LINE_LIMIT))
    try:
        yield mine, peer
    finally:
        await peer.close()
        mine[1].close()
        with suppress(OSError):
            await mine[1].wait_closed()


# meiva:note
# A temp directory short enough for AF_UNIX socket paths (about 104 bytes on macOS; pytest's `tmp_path` can be longer).
@pytest.fixture
def short_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="meiva-ls-", dir="/tmp") as name:
        yield Path(name)


# meiva:note
# A unix socket file nobody listens on at `path`: a connect is refused, as when a service died and left its socket behind.
def leftover_socket(path: Path) -> Path:
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    leftover.bind(str(path))
    leftover.close()
    return path
