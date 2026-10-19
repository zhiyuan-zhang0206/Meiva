from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from base.lm.harness.acp import AcpConnection, PermissionRequest

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")


def argv(*extra: str) -> list[str]:
    return [sys.executable, str(FAKE_AGENT), *extra]


async def connect(
    *, permission_allows: bool | None = None, extra: tuple[str, ...] = ()
) -> tuple[AcpConnection, list[PermissionRequest]]:
    seen: list[PermissionRequest] = []

    async def handler(request: PermissionRequest) -> str | None:
        seen.append(request)
        if permission_allows is None:
            return None
        allowed = request.option("allow_once")
        rejected = request.option("reject_once")
        return (
            (allowed.option_id if allowed else None)
            if permission_allows
            else (rejected.option_id if rejected else None)
        )

    connection = AcpConnection(
        argv(*extra), cwd=str(FAKE_AGENT.parent), environ={}, permission=handler
    )
    await connection.start()
    await connection.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
    return connection, seen


async def collect(connection: AcpConnection, *, count: int, timeout: float = 2.0) -> list[object]:
    received: list[object] = []

    async def reader() -> None:
        async for notification in connection.notifications():
            received.append(notification)
            if len(received) >= count:
                return

    await asyncio.wait_for(reader(), timeout=timeout)
    return received
