# meiva:note
# Unit tests for meiva.mcps._browser, the in-daemon client for the browser-mcp service. No real browser daemon: the line protocol runs over a real socket pair or a real unix socket under a short temp home. Mirrors the wrapper's protocol expectations: the service answers `list_tools` with a bare tool-dict list and `call_tool` with a CallToolResult dump, both as one JSON line.
from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import meiva.mcps._browser as browser_mod
from base.clock.tests.manual_timers import ManualTimers
from base.config import settings
from base.paths import chrome_mcp_socket
from meiva.mcps.tests.line_streams import LinePeer, leftover_socket, line_pair

CDP_PORT = 9333


@pytest.fixture
def browser_home() -> Iterator[Path]:
    """A home whose browser-mcp socket path fits AF_UNIX (about 104 bytes; pytest's tmp_path is longer)."""
    with tempfile.TemporaryDirectory(prefix="meiva-bh-", dir="/tmp") as name:
        yield Path(name)


def service_socket(home: Path, port: int = CDP_PORT) -> Path:
    """The browser-mcp socket of `home`, with its run directory created as the service would."""
    sock = chrome_mcp_socket(home, port)
    sock.parent.mkdir(parents=True, exist_ok=True)
    return sock


# meiva:note
# A session over a real socket pair whose far end answers each request line with the next of `responses` and records the request, so a test asserts on the wire. `requests` fills as the session speaks.
@asynccontextmanager
async def scripted_session(
    responses: list[dict[str, Any]],
) -> AsyncGenerator[tuple[browser_mod.BrowserLineSession, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    async def serve(peer: LinePeer) -> None:
        for response in responses:
            requests.append(await peer.receive())
            peer.send(response)
        await peer.close()

    async with line_pair() as ((reader, writer), peer):
        server = asyncio.create_task(serve(peer))
        try:
            yield browser_mod.BrowserLineSession(reader, writer), requests
        finally:
            server.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server


# ─── list_tools / call_tool protocol mapping ─────────────────────────────


async def test_list_tools_wraps_bare_tool_list() -> None:
    """The service answers list_tools with a bare tool-dict list (the wrapper
    validated each entry the same way); the session wraps it in the SDK
    ListToolsResult shape the daemon's request loop consumes."""
    answer: dict[str, Any] = {
        "id": 1,
        "ok": True,
        "result": [
            {"name": "navigate", "description": "go", "inputSchema": {}},
            {"name": "click", "description": "", "inputSchema": {}},
        ],
    }
    async with scripted_session([answer]) as (session, _):
        result = await session.list_tools()
    assert [t.name for t in result.tools] == ["navigate", "click"]
    assert result.tools[0].description == "go"


async def test_call_tool_maps_result_dump() -> None:
    """call_tool forwards tool+args and validates the CallToolResult dump."""
    answer = {
        "id": 1,
        "ok": True,
        "result": {
            "content": [{"type": "text", "text": "hello"}],
            "isError": False,
            "structuredContent": {"k": 1},
        },
    }
    async with scripted_session([answer]) as (session, requests):
        result = await session.call_tool("navigate", {"url": "https://x"})
    assert result.is_error is False
    assert result.content[0].text == "hello"  # type: ignore[union-attr]
    assert result.structured_content == {"k": 1}
    # the wire request carries the tool name + args
    assert requests == [
        {"id": 1, "method": "call_tool", "tool": "navigate", "args": {"url": "https://x"}}
    ]


async def test_list_tools_wire_payload() -> None:
    async with scripted_session([{"id": 1, "ok": True, "result": []}]) as (session, requests):
        await session.list_tools()
    assert requests == [{"id": 1, "method": "list_tools"}]


async def test_request_id_mismatch_raises() -> None:
    """A response whose id does not match the request means the stream
    desynced — fail loud instead of handing back another call's result. A
    session built without a re-dial path (unit test) has nothing to rebuild
    and simply raises."""
    async with scripted_session([{"id": 99, "ok": True, "result": []}]) as (session, _):
        with pytest.raises(RuntimeError, match="response id 99 != request 1"):
            await session.list_tools()


async def test_request_id_mismatch_reconnects_then_next_call_succeeds(
    browser_home: Path,
) -> None:
    """A desynced response rebuilds the socket (ids restart at 1) instead of
    poisoning the session: the call that observed the mismatch raises, and the
    next call succeeds on the fresh connection. The old socket is closed."""
    sock_path = service_socket(browser_home)

    connections: int = 0
    closed: int = 0
    rebuilt = asyncio.Event()
    all_closed = asyncio.Event()

    async def _handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        nonlocal connections, closed
        connections += 1
        rebuilt.set()
        desynced = connections == 1
        try:
            while line := await r.readline():
                req = json.loads(line)
                # The desynced connection answers with a stale id (a response
                # to an earlier request) — never the request's own id.
                resp: dict[str, Any] = {
                    "id": 99 if desynced else req["id"],
                    "ok": True,
                    "result": [],
                }
                w.write((json.dumps(resp) + "\n").encode())
                await w.drain()
        finally:
            w.close()
            closed += 1
            if closed == 2:
                all_closed.set()

    server = await asyncio.start_unix_server(_handle, path=str(sock_path))

    session, stack = await browser_mod.connect_browser_direct(browser_home, CDP_PORT)
    with pytest.raises(RuntimeError, match="response id 99 != request 1"):
        await session.list_tools()
    # The rebuilt connection serves subsequent calls normally.
    result = await session.list_tools()
    assert result.tools == []
    await asyncio.wait_for(rebuilt.wait(), timeout=2)  # the rebuild dialed a new socket
    assert connections == 2  # the mismatch triggered a rebuild
    # Stack teardown closes the CURRENT (rebuilt) socket, not the dead one.
    await stack.aclose()
    await asyncio.wait_for(all_closed.wait(), timeout=2)  # server saw both EOFs
    assert closed == 2
    server.close()
    await server.wait_closed()
    with contextlib.suppress(OSError):
        sock_path.unlink()


async def test_corrupt_line_reconnects_then_next_call_succeeds(
    browser_home: Path,
) -> None:
    """A non-JSON response line also means the stream is lost — rebuild the
    connection so a single corrupt line cannot brick it for good."""
    sock_path = service_socket(browser_home)

    connections: int = 0
    rebuilt = asyncio.Event()

    async def _handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        rebuilt.set()
        corrupt = connections == 1
        try:
            while line := await r.readline():
                if corrupt:
                    w.write(b"not json at all\n")
                else:
                    req = json.loads(line)
                    w.write(
                        (json.dumps({"id": req["id"], "ok": True, "result": []}) + "\n").encode()
                    )
                await w.drain()
        finally:
            w.close()

    server = await asyncio.start_unix_server(_handle, path=str(sock_path))

    session, stack = await browser_mod.connect_browser_direct(browser_home, CDP_PORT)
    with pytest.raises(RuntimeError, match="corrupted line"):
        await session.list_tools()
    result = await session.list_tools()
    assert result.tools == []
    await asyncio.wait_for(rebuilt.wait(), timeout=2)
    assert connections == 2
    await stack.aclose()
    server.close()
    await server.wait_closed()
    with contextlib.suppress(OSError):
        sock_path.unlink()


async def test_ok_false_raises_with_error() -> None:
    async with scripted_session([{"id": 1, "ok": False, "error": "upstream down"}]) as (
        session,
        _,
    ):
        with pytest.raises(RuntimeError, match="upstream down"):
            await session.list_tools()


async def test_eof_raises_connection_error() -> None:
    """A closed service connection (daemon died) surfaces as ConnectionError —
    the daemon's transport-error retry treats it as reconnectable."""
    async with scripted_session([]) as (session, _):
        with pytest.raises(ConnectionError):
            await session.list_tools()


# ─── connect + retry ─────────────────────────────────────────────────────


async def test_connect_browser_direct_dials_socket(browser_home: Path) -> None:
    """connect_browser_direct opens one unix connection and returns a session
    whose stack closes the writer on exit."""
    sock_path = service_socket(browser_home)

    async def _drop(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()

    server = await asyncio.start_unix_server(_drop, path=str(sock_path))

    session, stack = await browser_mod.connect_browser_direct(browser_home, CDP_PORT)
    assert isinstance(session, browser_mod.BrowserLineSession)
    await stack.aclose()
    server.close()
    await server.wait_closed()
    with contextlib.suppress(OSError):
        sock_path.unlink()


async def test_connect_browser_direct_retries_until_socket_appears(
    browser_home: Path,
) -> None:
    """A cold-start race (service socket not yet bound) retries instead of
    failing immediately — mirrors the wrapper's connect retry."""
    sock_path = service_socket(browser_home)

    server_future: asyncio.Future[asyncio.AbstractServer] = asyncio.Future()

    async def _late_server() -> None:
        await asyncio.sleep(0.08)

        async def _drop(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            w.close()

        server = await asyncio.start_unix_server(_drop, path=str(sock_path))
        server_future.set_result(server)
        await asyncio.sleep(5)

    task = asyncio.create_task(_late_server())
    try:
        session, stack = await browser_mod.connect_browser_direct(
            browser_home, CDP_PORT, attempts=20, delay_s=0.02
        )
        assert isinstance(session, browser_mod.BrowserLineSession)
        await stack.aclose()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(OSError):
            sock_path.unlink()


@pytest.mark.parametrize("failure", ["missing", "refused"])
async def test_dial_browser_exhaustion_has_no_trailing_sleep(
    browser_home: Path, failure: str
) -> None:
    timers = ManualTimers()
    absent = browser_home / "absent.sock"
    sock = str(absent if failure == "missing" else leftover_socket(browser_home / "dead.sock"))
    with pytest.raises(ConnectionError) as error:
        await browser_mod.dial_browser_mcp(sock, attempts=4, delay_s=0.5, asleep=timers.asleep)
    assert str(error.value).startswith(f"browser-mcp daemon not reachable at {sock}: ")
    assert error.value.__context__ is None
    assert timers.sleeps == [0.5] * 3


# meiva:note
# A path below a regular file fails with NotADirectoryError, which is no cold-start race.
async def test_dial_browser_non_retryable_error_passes_through(browser_home: Path) -> None:
    timers = ManualTimers()
    a_file = browser_home / "plain"
    a_file.write_text("x")
    with pytest.raises(NotADirectoryError):
        await browser_mod.dial_browser_mcp(
            str(a_file / "svc.sock"), attempts=4, delay_s=0.5, asleep=timers.asleep
        )
    assert timers.sleeps == []


async def test_call_tool_wire_carries_agent_id() -> None:
    """The daemon stamps the calling agent's id before each call, and the
    request carries it — the service keys page affinity per agent, so an exec
    subprocess child's fresh connection adopts the agent's selected page."""
    answer = {
        "id": 1,
        "ok": True,
        "result": {"content": [{"type": "text", "text": "hello"}], "isError": False},
    }
    async with scripted_session([answer]) as (session, requests):
        session.client_agent_id = 7
        result = await session.call_tool("take_snapshot", {})
    assert result.is_error is False
    assert requests == [
        {"id": 1, "method": "call_tool", "tool": "take_snapshot", "args": {}, "agent_id": 7}
    ]


async def test_call_tool_wire_omits_agent_id_when_unset() -> None:
    """No agent identity (daemon serving a non-agent client): the wire stays
    exactly the legacy shape — the service falls back to per-connection
    affinity."""
    answer = {
        "id": 1,
        "ok": True,
        "result": {"content": [{"type": "text", "text": "hello"}], "isError": False},
    }
    async with scripted_session([answer]) as (session, requests):
        await session.call_tool("take_snapshot", {})
    assert requests == [{"id": 1, "method": "call_tool", "tool": "take_snapshot", "args": {}}]


async def test_release_agent_chrome_pages_sends_release_and_reports_ok(
    browser_home: Path,
) -> None:
    """The exit-hook release dials the service once and sends the release
    request carrying the agent id; a truthful `ok` is returned."""
    sock_path = service_socket(browser_home, settings.services.browser_cdp_port)
    received: list[dict[str, Any]] = []

    async def _answer(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        line = await r.readline()
        received.append(json.loads(line))
        w.write((json.dumps({"id": 1, "ok": True, "result": {"page_id": 1}}) + "\n").encode())
        await w.drain()
        w.close()

    server = await asyncio.start_unix_server(_answer, path=str(sock_path))
    try:
        assert await browser_mod.release_agent_chrome_pages(7, browser_home) is True
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(OSError):
            sock_path.unlink()
    assert received == [{"id": 1, "method": "release_agent_page", "agent_id": 7}]


async def test_release_agent_chrome_pages_no_service_returns_false(
    browser_home: Path,
) -> None:
    """No browser service on this machine (socket absent) must degrade to a
    clean False — the exit path never raises on the cleanup nicety."""
    assert await browser_mod.release_agent_chrome_pages(7, browser_home) is False
