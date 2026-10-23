# meiva:note
# `release_browser_pages` dials the browser-mcp service of the home it is given.


from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from base.agents.lifecycle import release_browser_pages
from base.config import settings
from base.paths import chrome_mcp_socket


@pytest.fixture
def browser_home() -> Iterator[Path]:
    """A home whose browser-mcp socket path fits AF_UNIX (about 104 bytes; pytest's tmp_path is longer)."""
    with tempfile.TemporaryDirectory(prefix="meiva-rb-", dir="/tmp") as name:
        yield Path(name)


async def _serve(
    home: Path, reply: dict[str, Any], received: list[dict[str, Any]]
) -> tuple[asyncio.Server, Path]:
    sock = chrome_mcp_socket(home, settings.services.browser_cdp_port)
    sock.parent.mkdir(parents=True, exist_ok=True)

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(json.loads(await reader.readline()))
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(answer, path=str(sock)), sock


async def test_the_release_request_reaches_the_service_of_the_given_home(
    browser_home: Path,
) -> None:
    received: list[dict[str, Any]] = []
    server, sock = await _serve(browser_home, {"id": 1, "ok": True}, received)
    try:
        await release_browser_pages(7, home=browser_home)
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            sock.unlink()
    assert received == [{"id": 1, "method": "release_agent_page", "agent_id": 7}]


async def test_a_home_without_a_service_is_a_silent_no_op(browser_home: Path) -> None:
    await release_browser_pages(7, home=browser_home)


async def test_a_refusing_service_is_not_an_error(browser_home: Path) -> None:
    received: list[dict[str, Any]] = []
    server, sock = await _serve(browser_home, {"id": 1, "ok": False}, received)
    try:
        await release_browser_pages(7, home=browser_home)
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            sock.unlink()
    assert len(received) == 1
