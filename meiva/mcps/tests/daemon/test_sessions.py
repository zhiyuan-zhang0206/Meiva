from __future__ import annotations

import asyncio
import json
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import psutil
import pytest

import meiva.mcps._daemon as daemon_mod
from meiva.mcps.tests.daemon.daemon_support import (
    ask,
    fixture_entry,
    served_connection,
    started_pids,
    write_servers,
)


# meiva:note
# True once no live process has `pid`: the daemon released its child (a zombie awaiting its parent counts as gone).
async def process_gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
            return True
        await asyncio.sleep(0.05)
    return False


async def child_pid(session: Any) -> int:
    result = await session.call_tool("pid", {})
    return int(result.structured_content["result"])


# meiva:note
# First get connects the real server once; the second returns the cached session.
async def test_get_session_connects_lazily_and_caches(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(unit_home, {"fs": fixture_entry(fixture_spawn_log=spawn_log)})
    assert not spawn_log.exists()

    first = await daemon_mod.get_session("fs", scope)
    second = await daemon_mod.get_session("fs", scope)

    assert first is second
    assert scope.local.sessions["fs"] is first
    assert isinstance(scope.local.stacks["fs"], AsyncExitStack)
    assert started_pids(spawn_log) == [await child_pid(first)]
    await scope.local.close()


# meiva:note
# server not configured in mcp.json → ValueError immediately blows up (fail-fast, no silent
# connect).
async def test_get_session_raises_on_unknown_server(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    write_servers(unit_home, {"fs": fixture_entry()})
    with pytest.raises(ValueError, match="nope"):
        await daemon_mod.get_session("nope", scope)
    assert scope.local.sessions == {}


# meiva:note
# Multiple concurrent get_session(same server) start one server child (lock convergence).
async def test_concurrent_get_session_calls_share_one_connect(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(unit_home, {"fs": fixture_entry(fixture_spawn_log=spawn_log)})

    first, second = await asyncio.gather(
        daemon_mod.get_session("fs", scope), daemon_mod.get_session("fs", scope)
    )

    assert first is second
    assert len(started_pids(spawn_log)) == 1
    await scope.local.close()


# meiva:note
# A `"shared": true` server caches in the daemon-wide buckets and is wrapped in a serializing
# session — one stdio child for every connection.
async def test_a_shared_true_server_lives_in_the_daemon_wide_buckets(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    write_servers(unit_home, {"disc": fixture_entry(shared=True)})

    got = await daemon_mod.get_session("disc", scope)

    assert isinstance(got, daemon_mod.SerialSession)
    assert scope.local.sessions == {}
    assert scope.shared.sessions["disc"] is got
    assert "disc" in scope.shared.stacks
    await scope.shared.close()


# meiva:note
# Two clients on the shared daemon each get their own server child: agent A's `fs` session is
# invisible to agent B.
async def test_connections_never_share_session_state(
    unit_home: Path, daemon_wide: daemon_mod.DaemonWide
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(unit_home, {"fs": fixture_entry(fixture_spawn_log=spawn_log)})

    async with served_connection(daemon_wide) as first, served_connection(daemon_wide) as second:
        pids: list[int] = []
        for peer in (first, second):
            response = await ask(peer, "call_tool", "fs", tool="pid")
            pids.append(response["result"]["structuredContent"]["result"])

    assert pids[0] != pids[1]
    assert sorted(pids) == sorted(started_pids(spawn_log))
    assert daemon_wide.shared.sessions == {}
    assert all([await process_gone(pid) for pid in pids])


# meiva:note
# A dead agent must not leak its stdio children in the shared daemon.
async def test_closing_a_connection_releases_its_server_child(
    unit_home: Path, daemon_wide: daemon_mod.DaemonWide
) -> None:
    write_servers(unit_home, {"fs": fixture_entry()})
    async with served_connection(daemon_wide) as peer:
        response = await ask(peer, "call_tool", "fs", tool="pid")
        pid = response["result"]["structuredContent"]["result"]
        assert psutil.pid_exists(pid)

    assert await process_gone(pid)


# meiva:note
# Two connections on a shared server reach one child; closing a connection does not release it (it
# outlives every connection and is released at daemon shutdown).
async def test_shared_connections_share_one_child_that_outlives_them(
    unit_home: Path, daemon_wide: daemon_mod.DaemonWide
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(unit_home, {"disc": fixture_entry(shared=True, fixture_spawn_log=spawn_log)})

    pids: list[int] = []
    for _ in range(2):
        async with served_connection(daemon_wide) as peer:
            response = await ask(peer, "call_tool", "disc", tool="pid")
            pids.append(response["result"]["structuredContent"]["result"])

    assert pids[0] == pids[1]
    assert started_pids(spawn_log) == [pids[0]]
    assert psutil.pid_exists(pids[0])
    await daemon_wide.shared.close()
    assert await process_gone(pids[0])


async def test_invalidate_session_drops_the_cached_session_and_releases_its_child(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    write_servers(unit_home, {"fs": fixture_entry()})
    session = await daemon_mod.get_session("fs", scope)
    pid = await child_pid(session)

    await daemon_mod.invalidate_session("fs", scope)

    assert scope.local.sessions == {} and scope.local.stacks == {}
    assert await process_gone(pid)


async def test_invalidate_session_of_a_shared_server_clears_the_daemon_wide_buckets(
    unit_home: Path, scope: daemon_mod.Scope
) -> None:
    write_servers(unit_home, {"disc": fixture_entry(shared=True)})
    await daemon_mod.get_session("disc", scope)
    assert "disc" in scope.shared.sessions

    await daemon_mod.invalidate_session("disc", scope)

    assert "disc" not in scope.shared.sessions
    assert "disc" not in scope.shared.stacks


# meiva:note
# Invalidating an uncached server does nothing (no error).
async def test_invalidate_session_noop_when_not_cached(scope: daemon_mod.Scope) -> None:
    await daemon_mod.invalidate_session("nonexistent", scope)


# meiva:note
# Close each stack and clear both dicts; a stack that fails to close does not stop the next.
async def test_buckets_close_releases_every_stack_even_when_one_fails() -> None:
    closed: list[str] = []

    async def closes(name: str) -> None:
        closed.append(name)

    async def fails_to_close() -> None:
        closed.append("b")
        raise RuntimeError("close fail")

    stacks = {name: AsyncExitStack() for name in "abc"}
    stacks["a"].push_async_callback(closes, "a")
    stacks["b"].push_async_callback(fails_to_close)
    stacks["c"].push_async_callback(closes, "c")
    buckets = daemon_mod.Buckets(sessions={"a": "sa", "b": "sb", "c": "sc"}, stacks=stacks)

    await buckets.close()

    assert closed == ["a", "b", "c"]
    assert buckets.sessions == {} and buckets.stacks == {}


# meiva:note
# Concurrent calls on a shared session are serialized under one lock: the inner session sees one
# in-flight call at a time.
async def test_serial_session_serializes_concurrent_calls() -> None:
    from meiva.mcps._browser import BrowserLineSession
    from meiva.mcps.tests.line_streams import line_pair

    entered = 0
    in_flight = 0
    max_in_flight = 0
    release = asyncio.Event()

    async with line_pair() as ((reader, writer), peer):

        async def serve_two() -> None:
            nonlocal entered, in_flight, max_in_flight
            for _ in range(2):
                req = await peer.receive()
                entered += 1
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await release.wait()
                in_flight -= 1
                peer.send({"id": req["id"], "ok": True, "result": []})

        server_task = asyncio.create_task(serve_two())
        serial = daemon_mod.SerialSession(BrowserLineSession(reader, writer), asyncio.Lock())
        first = asyncio.create_task(serial.list_tools())
        await asyncio.sleep(0.01)
        second = asyncio.create_task(serial.list_tools())
        await asyncio.sleep(0.01)
        # meiva:note
        # the second call is blocked on the lock
        assert entered == 1
        release.set()
        await asyncio.gather(first, second, server_task)

        assert entered == 2
        # meiva:note
        # never two at once
        assert max_in_flight == 1


# meiva:note
# shared_kind returns the declared value, None when absent/unknown.
def test_shared_kind_reads_the_servers_declaration(unit_home: Path) -> None:
    write_servers(
        unit_home,
        {
            "a": {"command": "x", "shared": True},
            "b": {"command": "x", "shared": "browser"},
            "c": {"command": "x"},
        },
    )
    assert daemon_mod.shared_kind("a", home=unit_home) is True
    assert daemon_mod.shared_kind("b", home=unit_home) == "browser"
    assert daemon_mod.shared_kind("c", home=unit_home) is None
    assert daemon_mod.shared_kind("nope", home=unit_home) is None


# meiva:note
# Each tool request checks that its server remains enabled before reusing a warm session: disabling
# (or corrupting the overlay of) a server rejects later requests without cancelling the work already
# done.
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize(
    ("overlay", "reason"),
    [
        ('{"mcp_servers":{"fs":{"enabled":false}}}', "not configured"),
        ("{not json", "mcp_enabled.json"),
    ],
)
async def test_a_warm_session_is_rejected_after_the_overlay_changes(
    unit_home: Path,
    scope: daemon_mod.Scope,
    shared: bool,
    overlay: str,
    reason: str,
) -> None:
    effects = unit_home / "effects.log"
    write_servers(unit_home, {"fs": fixture_entry(shared=shared, fixture_effect_log=effects)})
    request = {"id": 1, "method": "call_tool", "params": {"server": "fs", "tool": "bump"}}

    assert (await daemon_mod.dispatch_with_retry(request, scope))["ok"] is True
    (unit_home / "mcp_enabled.json").write_text(overlay)
    response = await daemon_mod.dispatch_with_retry(request, scope)

    assert response["ok"] is False
    assert reason in str(response["error"])
    assert len(effects.read_text().splitlines()) == 1
    assert "fs" in (scope.shared if shared else scope.local).sessions
    await scope.local.close()
    await scope.shared.close()


@pytest.mark.parametrize(
    ("overlay", "reason"),
    [
        ("{not json", "mcp_enabled.json"),
        ('{"mcp_servers":{"fs":{"enabled":false}}}', "not configured"),
    ],
)
async def test_an_invalid_overlay_or_a_disabled_server_never_starts_a_process(
    unit_home: Path, scope: daemon_mod.Scope, overlay: str, reason: str
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(unit_home, {"fs": fixture_entry(fixture_spawn_log=spawn_log)})
    (unit_home / "mcp_enabled.json").write_text(overlay)

    response = await daemon_mod.dispatch_with_retry(
        {"id": 1, "method": "list_tools", "params": {"server": "fs"}}, scope
    )

    assert response["ok"] is False
    assert reason in str(response["error"])
    assert not spawn_log.exists()
    assert scope.local.sessions == {} and scope.shared.sessions == {}
    # meiva:note
    # the response is wire-encodable
    assert json.dumps(response)
