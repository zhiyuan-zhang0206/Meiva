# meiva:note
# What a slow call and a dying upstream do to the browser-mcp daemon as a whole: the
# call fails alone and the daemon stays; a death ends the daemon and says why.
from __future__ import annotations

import asyncio
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from mcp import ClientSession, types
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT

from base.clock.tests.fakes import system_clock
from services.desktop.browser.mcp_daemon import serve_with_upstream
from services.desktop.browser.mcp_upstream import DeathReason, UpstreamCallTimeoutError
from services.desktop.browser.tests.test_browser_mcp_daemon import FakeUpstream, new_daemon


# meiva:note
# Its first `take_snapshot` outlives the read timeout; the session still answers pings.
class SlowThenFineUpstream(FakeUpstream):
    def __init__(self) -> None:
        super().__init__()
        self.timeouts = 1

    async def send_ping(self) -> None:
        return None

    async def call_tool(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        if name == "take_snapshot" and self.timeouts:
            self.timeouts -= 1
            raise MCPError(code=REQUEST_TIMEOUT, message="Request 'tools/call' timed out")
        return await super().call_tool(name, args)


async def test_a_slow_call_costs_its_agent_the_call_and_nobody_the_browser() -> None:
    up = SlowThenFineUpstream()
    d = new_daemon(up)
    await d.call_tool_for_agent("new_page", {"url": "https://a.example/"}, 7)

    with pytest.raises(UpstreamCallTimeoutError):
        await d.call_tool_for_agent("take_snapshot", {}, 7)

    assert not d.dead.is_set()
    # meiva:note
    # the page stays selected
    assert d.pages.get_agent_page(7) == 1
    assert not (await d.call_tool_for_agent("take_snapshot", {}, 7)).is_error


class StubSession:
    def __init__(self, ping_error: Exception | None) -> None:
        self.ping_error = ping_error

    async def send_ping(self) -> None:
        if self.ping_error is not None:
            raise self.ping_error


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _serve(home: Path, session: StubSession, stop: asyncio.Event) -> None:
    await serve_with_upstream(
        home=home,
        browser_cdp_port=_free_port(),
        stop=stop,
        upstream=cast(ClientSession, session),
        current_stack=None,
        clock=system_clock(),
    )


# meiva:note
# A unix socket path only fits under a short directory on every host.
def _short_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="mm-", dir="/tmp"))


async def test_a_dead_upstream_ends_the_daemon_and_says_why(
    loguru_records: list[dict[str, Any]],
) -> None:
    home = _short_home()
    try:
        with pytest.raises(ExceptionGroup, match="unhandled errors"):
            await asyncio.wait_for(
                _serve(home, StubSession(anyio.ClosedResourceError()), asyncio.Event()), 20
            )
    finally:
        shutil.rmtree(home)

    assert any("daemon exiting: upstream_exited" in r["message"] for r in loguru_records)


async def test_a_stop_ends_the_daemon_as_stopped_not_as_a_death(
    loguru_records: list[dict[str, Any]],
) -> None:
    home = _short_home()
    stop = asyncio.Event()
    try:
        serving = asyncio.create_task(_serve(home, StubSession(None), stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(serving, 20)
    finally:
        shutil.rmtree(home)

    exits = [r for r in loguru_records if "daemon exiting" in r["message"]]
    assert [r["message"].rsplit(": ", 1)[1] for r in exits] == ["stopped"]
    assert exits[0]["level"].name == "INFO"


# meiva:note
# Every `take_snapshot` outlives the read timeout (raised at once here); pings answer.
class AlwaysSlowUpstream(FakeUpstream):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_attempts = 0

    async def send_ping(self) -> None:
        return None

    async def call_tool(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        if name == "take_snapshot":
            self.snapshot_attempts += 1
            raise MCPError(code=REQUEST_TIMEOUT, message="Request 'tools/call' timed out")
        return await super().call_tool(name, args)


async def test_queued_agents_stop_waiting_once_the_timeouts_end_the_generation() -> None:
    up = AlwaysSlowUpstream()
    d = new_daemon(up)
    agents = [7, 8, 9, 10]
    for agent in agents:
        await d.call_tool_for_agent("new_page", {"url": f"https://{agent}.example/"}, agent)

    outcomes = await asyncio.gather(
        *(d.call_tool_for_agent("take_snapshot", {}, agent) for agent in agents),
        return_exceptions=True,
    )

    # meiva:note
    # the first two time out alone, the third ends the generation, the fourth (already queued)
    # is refused without a fourth wait for the read timeout
    assert [type(o) for o in outcomes] == [
        UpstreamCallTimeoutError,
        UpstreamCallTimeoutError,
        RuntimeError,
        RuntimeError,
    ]
    assert up.snapshot_attempts == 3
    assert d.dead.is_set() and d.guard.reason is DeathReason.CALL_TIMEOUTS_REPEATED
