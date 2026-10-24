from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import psutil

import meiva.mcps._daemon as daemon_mod
from meiva.mcps._policy import RetryPolicy
from meiva.mcps.tests.daemon.daemon_support import (
    ask,
    fixture_entry,
    served_client,
    started_pids,
    write_servers,
)
from meiva.mcps.tests.line_streams import line_pair


async def test_handle_client_lists_tools(unit_home: Path, scope: daemon_mod.Scope) -> None:
    write_servers(unit_home, {"fs": fixture_entry()})
    async with served_client(scope) as peer:
        response = await ask(peer, "list_tools", "fs")
    assert response["id"] == 1 and response["ok"] is True
    names = [tool["name"] for tool in response["result"]]
    assert names == ["echo", "pid", "fail", "noop", "bump", "bump_then_die", "exit_soon"]
    echo = response["result"][0]
    assert echo["input_schema"]["properties"] == {"text": {"title": "Text", "type": "string"}}
    assert set(echo) == {"name", "description", "input_schema"}


async def test_handle_client_call_tool_returns_content(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    write_servers(unit_home, {"fs": fixture_entry()})
    async with served_client(scope) as peer:
        response = await ask(peer, "call_tool", "fs", tool="echo", args={"text": "hi"})
    assert response == {
        "id": 1,
        "ok": True,
        "result": {
            "content": [{"type": "text", "text": "hi"}],
            "isError": False,
            "structuredContent": {"result": "hi"},
        },
    }


# meiva:note
# isError=True: the daemon still answers ok=True and carries the full structure; the semantic layer
# (`is_error → raise`) lives on the client side.
async def test_a_tool_error_is_data_the_client_judges(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    write_servers(unit_home, {"fs": fixture_entry()})
    async with served_client(scope) as peer:
        response = await ask(peer, "call_tool", "fs", tool="fail")
    assert response["ok"] is True
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["type"] == "text"


async def test_a_tool_with_no_output_has_no_content(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    write_servers(unit_home, {"fs": fixture_entry()})
    async with served_client(scope) as peer:
        response = await ask(peer, "call_tool", "fs", tool="noop")
    assert response["ok"] is True
    assert response["result"]["content"] == []


# meiva:note
# The watchdog's liveness probe needs no config and no server: a slow MCP server must never make the
# daemon look dead.
async def test_ping_answers_without_opening_any_session(scope: daemon_mod.Scope) -> None:
    async with served_client(scope) as peer:
        peer.send({"id": 0, "method": "ping"})
        assert await peer.receive() == {"id": 0, "ok": True, "result": "pong"}
    assert scope.local.sessions == {}


async def test_an_unknown_method_is_an_error_response(scope: daemon_mod.Scope) -> None:
    async with served_client(scope) as peer:
        peer.send({"id": 3, "method": "nuke_database", "params": {"server": "fs"}})
        assert await peer.receive() == {
            "id": 3,
            "ok": False,
            "error": "Unknown method: nuke_database",
        }


# meiva:note
# Missing server param → ValueError → ok=False: the default path does not silently swallow errors,
# an empty server name is treated as unconfigured.
async def test_a_request_without_a_server_is_an_unconfigured_server_error(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    write_servers(unit_home, {"fs": fixture_entry()})
    async with served_client(scope) as peer:
        peer.send({"id": 5, "method": "list_tools", "params": {}})
        response = await peer.receive()
    assert response["id"] == 5 and response["ok"] is False
    assert "not configured" in response["error"]


# meiva:note
# Line-by-line resync: a garbage line does not break the connection.
async def test_bad_json_gets_a_parse_error_and_the_next_line_still_works(
    scope: daemon_mod.Scope,
) -> None:
    async with served_client(scope) as peer:
        peer.send_raw(b"garbage\n")
        first = await peer.receive()
        peer.send({"id": 99, "method": "ping"})
        second = await peer.receive()
    assert first["id"] is None and first["ok"] is False and "JSON parse error" in first["error"]
    assert second == {"id": 99, "ok": True, "result": "pong"}


async def test_the_client_going_away_closes_the_writer(scope: daemon_mod.Scope) -> None:
    async with line_pair() as ((reader, writer), peer):
        handler = asyncio.create_task(daemon_mod.handle_client(reader, writer, scope))
        await peer.close()
        await asyncio.wait_for(handler, 5)
        assert writer.is_closing()


# meiva:note
# A reset the kernel can raise on a real socket only under a dying peer: injected here as the read
# failing, which must end the connection, not the daemon.
async def test_a_connection_reset_ends_the_connection_quietly(scope: daemon_mod.Scope) -> None:
    class ResettingReader:
        async def readline(self) -> bytes:
            raise ConnectionResetError("peer reset")

    async with line_pair() as ((_, writer), _peer):
        await daemon_mod.handle_client(cast(asyncio.StreamReader, ResettingReader()), writer, scope)
        assert writer.is_closing()


# meiva:note
# Session selection that fails before a tool starts is retried on a fresh server.
async def test_a_listing_retries_after_the_first_server_start_dies(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(
        unit_home, {"fs": fixture_entry(fixture_spawn_log=spawn_log, fixture_die_starts=1)}
    )
    async with served_client(scope) as peer:
        response = await ask(peer, "list_tools", "fs")
    assert response["ok"] is True
    # meiva:note
    # the first start died before the handshake
    assert len(started_pids(spawn_log)) == 2


async def test_a_request_gives_up_after_the_attempts_allowed(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(
        unit_home, {"fs": fixture_entry(fixture_spawn_log=spawn_log, fixture_die_starts=99)}
    )
    async with served_client(scope) as peer:
        response = await ask(peer, "list_tools", "fs")
    assert response["ok"] is False
    assert len(started_pids(spawn_log)) == scope.config.retry.max_attempts
    assert scope.local.sessions == {}


# meiva:note
# The server exits between two requests: the second request's listing hits the dead session, drops
# it and succeeds on a fresh child.
async def test_a_dead_cached_session_is_rebuilt_by_the_retry(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(unit_home, {"fs": fixture_entry(fixture_spawn_log=spawn_log)})
    async with served_client(scope) as peer:
        assert (await ask(peer, "call_tool", "fs", tool="exit_soon"))["ok"] is True
        first_pid = started_pids(spawn_log)[0]
        deadline = time.monotonic() + 10
        while psutil.pid_exists(first_pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        response = await ask(peer, "list_tools", "fs")
    assert response["ok"] is True
    assert len(started_pids(spawn_log)) == 2


# meiva:note
# After a tool starts, a lost transport leaves the result UNKNOWN: the client is told so, the dead
# session is dropped, and the effect happened exactly once.
async def test_a_started_call_whose_reply_is_lost_is_never_replayed(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    effects = unit_home / "effects.log"
    spawn_log = unit_home / "spawns.log"
    write_servers(
        unit_home, {"fs": fixture_entry(fixture_spawn_log=spawn_log, fixture_effect_log=effects)}
    )
    async with served_client(scope) as peer:
        lost = await ask(peer, "call_tool", "fs", tool="bump_then_die")
        assert lost["ok"] is False
        assert "result unknown" in lost["error"]
        assert scope.local.sessions == {}
        again = await ask(peer, "call_tool", "fs", tool="bump")
    # meiva:note
    # one per call, no replay
    assert effects.read_text().splitlines() == ["effect", "effect"]
    assert again["result"]["structuredContent"] == {"result": 2}
    assert len(started_pids(spawn_log)) == 2


# meiva:note
# With a retry delay that would outlast the test, a retried request would time out here.
async def test_an_error_that_is_not_a_transport_failure_never_retries(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    slow = replace(scope, config=replace(scope.config, retry=RetryPolicy(3, 600.0, 600.0)))
    write_servers(unit_home, {"fs": fixture_entry()})
    response = await asyncio.wait_for(
        daemon_mod.dispatch_with_retry(
            {"id": 4, "method": "call_tool", "params": {"server": "nope", "tool": "x"}}, slow
        ),
        10,
    )
    assert response["ok"] is False
    error = str(response["error"])
    assert "ValueError" in error and "nope" in error
