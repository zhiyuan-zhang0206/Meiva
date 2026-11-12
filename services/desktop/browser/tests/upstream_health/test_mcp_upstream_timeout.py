import asyncio
import sys
from pathlib import Path
from typing import cast

import pytest
from mcp import ClientSession, StdioServerParameters

from services.desktop.browser.mcp_upstream import (
    StoppingError,
    initialize_until_stop_or_timeout,
    open_upstream,
    upstream_server_params,
)


class Initializer:
    def __init__(self, *, blocked: bool = False, error: Exception | None = None) -> None:
        self.blocked = blocked
        self.error = error
        self.cancelled = False
        self.calls = 0

    async def initialize(self) -> None:
        self.calls += 1
        try:
            if self.blocked:
                await asyncio.Event().wait()
            if self.error is not None:
                raise self.error
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_initialization_reads_current_timeout_for_each_race() -> None:
    timeout = 0.01
    values: list[float] = []

    def reader() -> float:
        values.append(timeout)
        return timeout

    session = Initializer(blocked=True)
    assert values == []
    with pytest.raises(TimeoutError, match="initialize"):
        await initialize_until_stop_or_timeout(
            cast(ClientSession, session), asyncio.Event(), connect_timeout_reader=reader
        )
    assert session.cancelled
    timeout = 1.0
    session.blocked = False
    await initialize_until_stop_or_timeout(
        cast(ClientSession, session), asyncio.Event(), connect_timeout_reader=reader
    )
    assert session.calls == 2
    assert values == [0.01, 1.0]


async def test_stop_still_cancels_pending_initialization() -> None:
    stop = asyncio.Event()
    stop.set()
    session = Initializer(blocked=True)
    with pytest.raises(StoppingError):
        await initialize_until_stop_or_timeout(
            cast(ClientSession, session), stop, connect_timeout_reader=lambda: 10.0
        )
    assert session.cancelled


async def test_original_initialization_error_is_preserved() -> None:
    error = RuntimeError("initialize failed")
    session = Initializer(error=error)
    with pytest.raises(RuntimeError) as observed:
        await initialize_until_stop_or_timeout(
            cast(ClientSession, session), asyncio.Event(), connect_timeout_reader=lambda: 1.0
        )
    assert observed.value is error


async def test_create_upstream_passes_reader_and_closes_failed_connect(
    tmp_path: Path,
) -> None:
    params = upstream_server_params("http://example.test")
    assert params.command == "npx"
    assert "http://example.test" in params.args

    marker = tmp_path / "stdio_lifecycle.txt"
    child_code = (
        "import pathlib, sys\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "marker.write_text('opened\\n', encoding='utf-8')\n"
        "try:\n"
        "    sys.stdin.read()\n"
        "finally:\n"
        "    with marker.open('a', encoding='utf-8') as f:\n"
        "        f.write('closed\\n')\n"
    )
    reads: list[float] = []

    def timeout_reader() -> float:
        reads.append(0.05)
        return 0.05

    with pytest.raises(TimeoutError):
        await open_upstream(
            StdioServerParameters(command=sys.executable, args=["-c", child_code]),
            asyncio.Event(),
            connect_timeout_reader=timeout_reader,
        )
    assert reads == [0.05]
    assert marker.read_text(encoding="utf-8").splitlines() == ["opened", "closed"]
