from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path

import psutil

import meiva.mcps._daemon as daemon_mod
from base.paths import mcp_daemon_shared_socket
from meiva.mcps.tests.daemon.daemon_support import (
    daemon_config,
    fixture_entry,
    started_pids,
    write_servers,
)
from meiva.mcps.tests.line_streams import short_dir as short_dir


# meiva:note
# Wait for the daemon task to listen on `socket_path` and return a connected (reader, writer). Each round also checks the task itself: a startup failure surfaces at once instead of as a vague "never listened".
async def connect_when_listening(
    daemon_task: asyncio.Task[None], socket_path: str, *, timeout: float = 10.0
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if daemon_task.done():
            raise RuntimeError(f"daemon exited during startup: {daemon_task.exception()!r}")
        if Path(socket_path).is_socket():
            with contextlib.suppress(FileNotFoundError, ConnectionRefusedError):
                return await asyncio.open_unix_connection(socket_path)
        await asyncio.sleep(0.02)
    raise RuntimeError(f"daemon never listened on {socket_path}")


async def stop(daemon_task: asyncio.Task[None]) -> None:
    daemon_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await daemon_task


# meiva:note
# A leftover file at the socket path (a crash) is cleared on startup; the real socket then carries
# the JSON-line protocol end to end, down to a real MCP server.
async def test_run_daemon_replaces_a_leftover_file_and_serves_over_the_socket(
    unit_home: Path, short_dir: Path
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(unit_home, {"fs": fixture_entry(fixture_spawn_log=spawn_log)})
    socket_path = str(short_dir / "d.sock")
    Path(socket_path).write_text("leftover")

    daemon_task = asyncio.create_task(daemon_mod.run_daemon(socket_path, daemon_config(unit_home)))
    reader, writer = await connect_when_listening(daemon_task, socket_path)
    try:
        request = {"id": 42, "method": "list_tools", "params": {"server": "fs"}}
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 30))
    finally:
        writer.close()
        await stop(daemon_task)

    assert response["id"] == 42 and response["ok"] is True
    assert response["result"][0]["name"] == "echo"
    assert not Path(socket_path).exists()


# meiva:note
# The path helpers derive paths and create nothing: the daemon that binds owns the directory.
async def test_run_daemon_creates_the_directory_of_its_socket(short_dir: Path) -> None:
    home = short_dir / "home"
    socket_path = mcp_daemon_shared_socket(home)
    assert not home.exists()

    daemon_task = asyncio.create_task(daemon_mod.run_daemon(socket_path, daemon_config(short_dir)))
    try:
        _, writer = await connect_when_listening(daemon_task, socket_path)
        writer.close()
    finally:
        await stop(daemon_task)
        shutil.rmtree(home, ignore_errors=True)

    assert home.exists() is False


# meiva:note
# The listening socket and the daemon-wide shared children are released on every exit path,
# including a cancel of the parent task.
async def test_a_stopped_daemon_releases_its_shared_children_and_its_socket(
    unit_home: Path, short_dir: Path
) -> None:
    spawn_log = unit_home / "spawns.log"
    write_servers(unit_home, {"disc": fixture_entry(shared=True, fixture_spawn_log=spawn_log)})
    socket_path = str(short_dir / "d.sock")

    daemon_task = asyncio.create_task(daemon_mod.run_daemon(socket_path, daemon_config(unit_home)))
    reader, writer = await connect_when_listening(daemon_task, socket_path)
    request = {"id": 1, "method": "call_tool", "params": {"server": "disc", "tool": "pid"}}
    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()
    json.loads(await asyncio.wait_for(reader.readline(), 30))
    [pid] = started_pids(spawn_log)
    assert psutil.pid_exists(pid)
    writer.close()

    await stop(daemon_task)

    deadline = asyncio.get_running_loop().time() + 10
    while psutil.pid_exists(pid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert not psutil.pid_exists(pid)
    assert not Path(socket_path).exists()
