from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import psutil

from base.lm.harness.acp.transport import AcpConnection
from base.native_process.ownership import OwnedProcess


async def wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


@dataclass(frozen=True)
class EofPeer:
    connection: AcpConnection
    stdin_closed: Path
    release: Path


@asynccontextmanager
async def eof_peer(root: Path, *, gate_exit: bool) -> AsyncGenerator[EofPeer]:
    connection = AcpConnection(
        [
            sys.executable,
            str(Path(__file__).with_name("eof_child.py")),
            str(root),
            "gated" if gate_exit else "stdin",
        ],
        cwd=str(root),
        environ={},
    )
    release = root / "release"
    identity: OwnedProcess | None = None
    try:
        await connection.start()
        assert connection.pid is not None
        identity = OwnedProcess.capture(psutil.Process(connection.pid))
        async with asyncio.timeout(8):
            await wait_until(lambda: (root / "ready").exists() and connection.closed)
            assert connection.returncode is None
            yield EofPeer(connection, root / "stdin-closed", release)
    finally:
        release.touch()
        try:
            await asyncio.wait_for(connection.close(), 3)
            await wait_until(lambda: connection.returncode is not None)
        finally:
            if identity is not None and identity.live():
                identity.send_signal(signal.SIGKILL)
                await wait_until(lambda: connection.returncode is not None)
        assert identity is None or not identity.live()
