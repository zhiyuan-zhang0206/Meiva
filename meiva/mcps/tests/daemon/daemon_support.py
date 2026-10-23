# meiva:note
# Builders and fixtures for the daemon tests. The daemon runs for real: its sessions are real MCP servers (`mcp_fixture_server.py` over stdio, an in-loop streamable-http server on port 0), its clients are real socket pairs, and its configuration is a `DaemonConfig` value with the retry delays set to zero.
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import meiva.mcps._daemon as daemon_mod
from meiva.mcps._policy import DaemonConfig, RetryPolicy
from meiva.mcps.tests.line_streams import LinePeer, line_pair

FIXTURE_SERVER = Path(__file__).with_name("mcp_fixture_server.py")
IDLE_PROCESS = Path(__file__).with_name("idle_process.py")
CDP_PORT = 9333


# meiva:note
# A daemon configuration for tests: connects bounded at 15 s, a display present, retries without waiting. `home` is the test's own home, so the stale-daemon reaper only ever matches processes of this test.
def daemon_config(home: Path, *, max_attempts: int = 3) -> DaemonConfig:
    return DaemonConfig(
        connect_timeout_seconds=15.0,
        display_available=True,
        home=home,
        project_root=home,
        browser_cdp_port=CDP_PORT,
        socket_probe_timeout_seconds=0.3,
        retry=RetryPolicy(max_attempts=max_attempts, base_delay_s=0.0, max_delay_s=0.0),
    )


# meiva:note
# The test's own daemon configuration over its home.
@pytest.fixture
def config(unit_home: Path) -> DaemonConfig:
    return daemon_config(unit_home)


# meiva:note
# The daemon-wide state of one fresh daemon (what `run_daemon` builds once).
@pytest.fixture
def daemon_wide(config: DaemonConfig) -> daemon_mod.DaemonWide:
    return daemon_mod.DaemonWide(config=config)


# meiva:note
# One client connection's scope: its own empty buckets over the daemon-wide state.
@pytest.fixture
def scope(daemon_wide: daemon_mod.DaemonWide) -> daemon_mod.Scope:
    return daemon_mod.Scope(
        local=daemon_mod.Buckets(),
        shared=daemon_wide.shared,
        oauth_locks=daemon_wide.oauth_locks,
        config=daemon_wide.config,
    )


def write_servers(home: Path, servers: dict[str, dict[str, Any]]) -> None:
    (home / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# meiva:note
# A `.mcp.json` entry that launches the fixture server; `env` steers it (see `mcp_fixture_server.py`).
def fixture_entry(*, shared: bool = False, **env: Path | int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "command": sys.executable,
        "args": [str(FIXTURE_SERVER)],
        "env": {name.upper(): str(value) for name, value in env.items()},
    }
    if shared:
        entry["shared"] = True
    return entry


def started_pids(spawn_log: Path) -> list[int]:
    return [int(pid) for pid in spawn_log.read_text(encoding="utf-8").splitlines()]


# meiva:note
# Serve one client over a real socket pair with `handle_client` and hand the test the far end. Closing the far end ends the connection, so leaving the block ends the handler; the scope's sessions are released with it, so no server child outlives the test.
@asynccontextmanager
async def served_client(scope: daemon_mod.Scope) -> AsyncGenerator[LinePeer]:
    async with line_pair() as ((reader, writer), peer):
        handler = asyncio.create_task(daemon_mod.handle_client(reader, writer, scope))
        try:
            yield peer
        finally:
            await peer.close()
            await asyncio.wait_for(handler, 30)
            await scope.local.close()
            await scope.shared.close()


# meiva:note
# The same through `handle_connection`: the connection owns its own scope over the daemon-wide state and closes its sessions when it ends.
@asynccontextmanager
async def served_connection(daemon: daemon_mod.DaemonWide) -> AsyncGenerator[LinePeer]:
    async with line_pair() as ((reader, writer), peer):
        handler = asyncio.create_task(daemon_mod.handle_connection(reader, writer, daemon))
        try:
            yield peer
        finally:
            await peer.close()
            await asyncio.wait_for(handler, 30)


# meiva:note
# One request over an open client and its response, with a generous bound for a first spawn.
async def ask(peer: LinePeer, method: str, server: str = "", **params: Any) -> dict[str, Any]:
    request: dict[str, Any] = {"id": 1, "method": method, "params": {"server": server, **params}}
    peer.send(request)
    return await peer.receive(timeout=30)
