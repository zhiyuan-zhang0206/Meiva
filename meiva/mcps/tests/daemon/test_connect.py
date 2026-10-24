from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import psutil
import pytest
from mcp.types import TextContent

import meiva.mcps._browser as browser_mod
import meiva.mcps._computer as computer_mod
import meiva.mcps._daemon as daemon_mod
from base.paths import chrome_mcp_socket, computer_mcp_socket
from meiva.mcps._policy import DaemonConfig
from meiva.mcps.config import MCPError
from meiva.mcps.tests.daemon.daemon_support import (
    CDP_PORT,
    IDLE_PROCESS,
    ask,
    daemon_config,
    fixture_entry,
    served_connection,
    write_servers,
)
from meiva.mcps.tests.daemon.http_server import http_mcp_server
from meiva.mcps.tests.line_streams import short_dir as short_dir


async def test_an_unmet_requirement_is_reported_before_anything_starts(
    unit_home: Path, config: DaemonConfig
) -> None:
    spawn_log = unit_home / "spawns.log"
    entry = fixture_entry(fixture_spawn_log=spawn_log)
    write_servers(unit_home, {"chrome": {**entry, "requires": {"display": True}}})

    with pytest.raises(MCPError, match="requires a display"):
        await daemon_mod.connect_server("chrome", replace(config, display_available=False), {})

    assert not spawn_log.exists()


async def test_a_met_requirement_connects(unit_home: Path, config: DaemonConfig) -> None:
    write_servers(unit_home, {"chrome": {**fixture_entry(), "requires": {"display": True}}})
    session, stack = await daemon_mod.connect_server("chrome", config, {})
    assert session is not None
    await stack.aclose()


async def test_an_invalid_requirement_declaration_fails_before_the_host_is_asked(
    unit_home: Path, config: DaemonConfig
) -> None:
    write_servers(unit_home, {"chrome": {**fixture_entry(), "requires": {"gpu": True}}})
    with pytest.raises(MCPError, match="unknown requires key"):
        await daemon_mod.connect_server("chrome", config, {})


# meiva:note
# An unrecognized `shared` value fails fast instead of silently falling back to a per-connection
# child (which would defeat the declared intent).
async def test_an_unknown_shared_value_fails_fast(unit_home: Path, config: DaemonConfig) -> None:
    write_servers(unit_home, {"weird": {"command": "x", "shared": "mars"}})
    with pytest.raises(ValueError, match="unknown shared value 'mars'"):
        await daemon_mod.connect_server("weird", config, {})


async def test_a_stdio_server_connects_and_its_stack_releases_the_child(
    unit_home: Path, config: DaemonConfig
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(unit_home, {"fs": fixture_entry(fixture_spawn_log=spawn_log)})

    session, stack = await daemon_mod.connect_server("fs", config, {})
    tools = await session.list_tools()
    pid = int(spawn_log.read_text().split()[0])
    assert psutil.pid_exists(pid)
    await stack.aclose()

    assert [tool.name for tool in tools.tools][:2] == ["echo", "pid"]
    deadline = time.monotonic() + 10
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not psutil.pid_exists(pid)


async def test_a_command_that_cannot_start_raises(unit_home: Path, config: DaemonConfig) -> None:
    write_servers(unit_home, {"gone": {"command": str(unit_home / "no-such-server")}})
    with pytest.raises(FileNotFoundError):
        await daemon_mod.connect_server("gone", config, {})


async def test_a_server_that_never_handshakes_times_out_and_leaves_no_child(
    unit_home: Path, config: DaemonConfig
) -> None:
    marker = "handshake-never-completes-marker"
    write_servers(
        unit_home,
        {"mute": {"command": sys.executable, "args": [str(IDLE_PROCESS), marker]}},
    )
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        await daemon_mod.connect_server("mute", replace(config, connect_timeout_seconds=0.5), {})

    assert time.monotonic() - started < 15
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not any(
            marker in " ".join(p.info["cmdline"] or []) for p in psutil.process_iter(["cmdline"])
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the half-started child outlived the failed connect")


async def test_a_url_server_connects_over_http(unit_home: Path, config: DaemonConfig) -> None:
    async with http_mcp_server() as remote:
        write_servers(unit_home, {"remote": {"url": remote.url}})
        session, stack = await daemon_mod.connect_server("remote", config, {})
        tools = await session.list_tools()
        reply = await session.call_tool("echo", {"text": "over http"})
        await stack.aclose()
    assert [tool.name for tool in tools.tools] == ["echo"]
    block = reply.content[0]
    assert isinstance(block, TextContent) and block.text == "over http"


async def test_static_headers_ride_on_every_http_request(
    unit_home: Path, config: DaemonConfig
) -> None:
    async with http_mcp_server() as remote:
        write_servers(
            unit_home,
            {"remote": {"url": remote.url, "headers": {"Authorization": "Bearer k"}}},
        )
        _, stack = await daemon_mod.connect_server("remote", config, {})
        await stack.aclose()
    assert remote.seen_headers
    assert all(headers.get("authorization") == "Bearer k" for headers in remote.seen_headers)


async def test_a_url_server_without_headers_sends_none(
    unit_home: Path, config: DaemonConfig
) -> None:
    async with http_mcp_server() as remote:
        write_servers(unit_home, {"remote": {"url": remote.url}})
        _, stack = await daemon_mod.connect_server("remote", config, {})
        await stack.aclose()
    assert all("authorization" not in headers for headers in remote.seen_headers)


# meiva:note
# A dead endpoint raises (no retry here) and leaves no HTTP client behind.
async def test_a_dead_endpoint_fails_fast(unit_home: Path, config: DaemonConfig) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    write_servers(unit_home, {"remote": {"url": f"http://127.0.0.1:{port}/mcp"}})
    with pytest.raises(ExceptionGroup) as error:
        await daemon_mod.connect_server("remote", replace(config, connect_timeout_seconds=5.0), {})
    assert [type(e).__name__ for e in error.value.exceptions] == ["ConnectError"]


# meiva:note
# Serve a line-protocol service on `path` that answers every request with an empty ok result after `delay_s`, recording what it received.
async def line_service(
    path: Path, received: list[dict[str, Any]], *, delay_s: float = 0.0
) -> asyncio.AbstractServer:
    path.parent.mkdir(parents=True, exist_ok=True)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                request = json.loads(line)
                received.append(request)
                await asyncio.sleep(delay_s)
                result: object = (
                    [] if request["method"] == "list_tools" else {"content": [], "isError": False}
                )
                writer.write(
                    (
                        json.dumps({"id": request["id"], "ok": True, "result": result}) + "\n"
                    ).encode()
                )
                await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_unix_server(serve, path=str(path))


# meiva:note
# A `"shared": "browser"` server needs no stdio child: connect_server dials the service.
async def test_a_browser_server_is_dialed_directly(unit_home: Path, short_dir: Path) -> None:
    config = daemon_config(short_dir)
    write_servers(unit_home, {"chrome": {"command": "must-not-run", "shared": "browser"}})
    received: list[dict[str, Any]] = []
    server = await line_service(chrome_mcp_socket(short_dir, CDP_PORT), received)
    try:
        session, stack = await daemon_mod.connect_server("chrome", config, {})
        assert isinstance(session, browser_mod.BrowserLineSession)
        assert (await session.list_tools()).tools == []
        await stack.aclose()
    finally:
        server.close()
        await server.wait_closed()
    assert [request["method"] for request in received] == ["list_tools"]


async def test_a_computer_server_is_dialed_directly(unit_home: Path, short_dir: Path) -> None:
    config = daemon_config(short_dir)
    write_servers(
        unit_home, {"computer_use": {"command": "must-not-run", "shared": "computer_use"}}
    )
    received: list[dict[str, Any]] = []
    server = await line_service(Path(computer_mcp_socket(short_dir)), received)
    try:
        session, stack = await daemon_mod.connect_server("computer_use", config, {})
        assert isinstance(session, computer_mod.ComputerLineSession)
        await stack.aclose()
    finally:
        server.close()
        await server.wait_closed()


# meiva:note
# Two connections calling the browser server concurrently both succeed.
#
# Regression for the id-desync bug (browser-mcp daemon response id N != request N+2, permanently):
# the browser session used to live in the daemon-wide buckets, so concurrent connections multiplexed
# on ONE socket with ONE shared id counter and corrupted each other's response stream. Each
# connection must dial its own socket.
async def test_browser_connections_each_keep_their_own_socket(
    unit_home: Path, short_dir: Path
) -> None:
    daemon = daemon_mod.DaemonWide(config=daemon_config(short_dir))
    write_servers(unit_home, {"chrome": {"command": "must-not-run", "shared": "browser"}})
    received: list[dict[str, Any]] = []
    server = await line_service(chrome_mcp_socket(short_dir, CDP_PORT), received, delay_s=0.05)

    async def one_connection() -> dict[str, Any]:
        async with served_connection(daemon) as peer:
            return await ask(
                peer, "call_tool", "chrome", tool="navigate", args={"url": "https://x"}
            )

    try:
        first, second = await asyncio.gather(one_connection(), one_connection())
    finally:
        server.close()
        await server.wait_closed()
    assert first["ok"] is True and second["ok"] is True
    # meiva:note
    # per-connection sockets, none shared
    assert daemon.shared.sessions == {}


# meiva:note
# The agent id of the client's request is stamped onto the computer session before every call_tool,
# so the computer daemon can gate and audit per agent (the line payload carries it).
async def test_the_computer_service_hears_which_agent_called(
    unit_home: Path, short_dir: Path
) -> None:
    daemon = daemon_mod.DaemonWide(config=daemon_config(short_dir))
    write_servers(unit_home, {"computer_use": {"command": "x", "shared": "computer_use"}})
    received: list[dict[str, Any]] = []
    server = await line_service(Path(computer_mcp_socket(short_dir)), received)
    try:
        async with served_connection(daemon) as peer:
            peer.send(
                {
                    "id": 1,
                    "method": "call_tool",
                    "params": {"server": "computer_use", "tool": "click", "args": {"x": 1}},
                    "agent_id": 42,
                }
            )
            response = await peer.receive(timeout=30)
    finally:
        server.close()
        await server.wait_closed()
    assert response["ok"] is True
    assert [(r["tool"], r["agent_id"]) for r in received] == [("click", 42)]


# meiva:note
# A client that sent no agent id (the service then keys page affinity per connection) carries none
# on the wire.
async def test_a_request_without_an_agent_id_reaches_the_browser_service_without_one(
    unit_home: Path, short_dir: Path
) -> None:
    daemon = daemon_mod.DaemonWide(config=daemon_config(short_dir))
    write_servers(unit_home, {"chrome": {"command": "x", "shared": "browser"}})
    received: list[dict[str, Any]] = []
    server = await line_service(chrome_mcp_socket(short_dir, CDP_PORT), received)
    try:
        async with served_connection(daemon) as peer:
            await ask(peer, "call_tool", "chrome", tool="navigate")
    finally:
        server.close()
        await server.wait_closed()
    assert "agent_id" not in received[0]
