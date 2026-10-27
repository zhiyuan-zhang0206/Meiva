"""Per-agent computer MCP bridge: stdio MCP server in front of the shared service.

Speaks the standard MCP protocol to one agent (over stdio) and forwards every
call over a Unix socket to the per-machine `services.desktop.computer.mcp_daemon`, which
executes desktop actions through the signed permissions helper and audits them.
The tool list passes through verbatim from the daemon (single source of truth).
`agent_id` is stamped on every request from this process's identity, so the
audit stream carries the acting agent.

Wired in meiva_builtins/mcps/computer_use/.mcp.json as the compatibility command for older daemons
(the MCP daemon's primary path dials the service directly — see
meiva/mcps/_computer.py). Takes no arguments (the socket path is derived from
settings, matching the daemon). The MCP daemon spawns it with cwd pinned to the
repo root (`meiva/mcps/config.py:server_cwd`), so the relative interpreter path
resolves there:

    .venv/bin/python -m services.desktop.computer.mcp_wrapper
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from base.config import ConfigBoot
from base.host.env.agent_env import AGENT_ID_ENV
from base.paths import computer_mcp_socket, meiva_home
from services.desktop.browser.mcp_socket_bridge import (
    ReconnectingLink,
    SocketLink,
    dial_unix_socket,
)


def _agent_id() -> int | None:
    """This process's agent identity (None when running outside an agent)."""
    import os

    raw = os.environ.get(AGENT_ID_ENV)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


class Link(SocketLink):
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        timeout_reader: Callable[[], float],
        agent_id: int | None,
    ) -> None:
        super().__init__(
            reader,
            writer,
            service_label="computer MCP daemon",
            timeout_reader=timeout_reader,
            extra_fields={"agent_id": agent_id},
        )


class ReconnectingSocketLink(ReconnectingLink):
    def __init__(
        self, *, sock: str, timeout_reader: Callable[[], float], agent_id: int | None
    ) -> None:
        super().__init__(
            lambda: dial_unix_socket(sock, service_label="computer MCP daemon"),
            lambda reader, writer: Link(
                reader, writer, timeout_reader=timeout_reader, agent_id=agent_id
            ),
            max_attempts=6,
            base_delay=0.5,
            close_on_error=lambda _exc: True,
        )


async def _serve(*, sock: str, timeout_reader: Callable[[], float]) -> None:
    link = ReconnectingSocketLink(sock=sock, timeout_reader=timeout_reader, agent_id=_agent_id())

    async def _list_tools(_ctx: object, _params: object) -> types.ListToolsResult:
        tools: Any = await link.request({"method": "list_tools"})
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in tools])

    async def _call_tool(_ctx: object, params: types.CallToolRequestParams) -> types.CallToolResult:
        result = await link.request(
            {"method": "call_tool", "tool": params.name, "args": params.arguments or {}}
        )
        return types.CallToolResult.model_validate(result)

    server: Server[Any] = Server("computer_use", on_list_tools=_list_tools, on_call_tool=_call_tool)

    async with stdio_server() as (server_read, server_write):
        await server.run(server_read, server_write, server.create_initialization_options())


def timeout_reader(config: ConfigBoot) -> Callable[[], float]:
    config.boot()
    return lambda: config.view.sandbox.mcp_connect_timeout_seconds


def main() -> None:
    sock = str(computer_mcp_socket(meiva_home()))
    asyncio.run(_serve(sock=sock, timeout_reader=timeout_reader(ConfigBoot())))


if __name__ == "__main__":
    main()
