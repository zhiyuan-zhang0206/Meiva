"""Per-agent chrome MCP bridge: stdio MCP server in front of the shared service.

Speaks the standard MCP protocol to one agent (over stdio) and forwards every
call over a Unix socket to the per-machine `services.desktop.browser.mcp_daemon`, which
owns the single `chrome-devtools-mcp` upstream attached to the shared headed
Chrome. The catalog adds a host argument to the upstream tool schemas. This process is thin and per-agent;
the heavy upstream + the page-affinity / cold-start logic live in the daemon, so
one Chrome client is shared instead of one per agent.

Wired in meiva_builtins/mcps/chrome/.mcp.json; takes no arguments (the socket path is derived
from settings, matching the daemon). The MCP daemon spawns it with cwd pinned to
the repo root (`meiva/mcps/config.py:server_cwd`), so the relative interpreter path
resolves there — launched directly rather than through `uv run`, which would hang
one resident wrapper process per agent:

    .venv/bin/python -m services.desktop.browser.mcp_wrapper
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from base.agents.context.browser_identity import BROWSER_CALLER_ENV
from base.clock import SYSTEM_TIMERS
from base.config import ConfigBoot
from base.host.env.agent_env import AGENT_ID_ENV
from base.host.net.resilience import retry_asleep
from base.host.system.hostport import Host
from base.lm.harness.registration import HANDLE_ENV
from base.paths import chrome_mcp_socket, meiva_home
from services.desktop.browser.routing.mcp_socket_bridge import (
    ReconnectingLink,
    SocketLink,
    dial_unix_socket,
    is_transport_error,
)

from .routing.boot import browser_routes_from_boot
from .routing.session import BrowserRoutes, RoutedBrowserSession

# meiva:note
# The daemon explicitly rejected the request before forwarding it upstream.
_UPSTREAM_DOWN_MSG = "chrome upstream session is down; restart browser-mcp before continuing"


class Link(SocketLink):
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        timeout_reader: Callable[[], float],
    ) -> None:
        super().__init__(
            reader,
            writer,
            service_label="chrome MCP daemon",
            timeout_reader=timeout_reader,
            extra_fields={},
        )


# meiva:note
# The connect retry and the reconnect loop both wait through the callable the caller names, so one
# value drives every wait this wrapper makes.
def _connector(
    socket_path: Path,
    *,
    asleep: Callable[[float], Awaitable[None]] = retry_asleep,
) -> Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]:
    async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await dial_unix_socket(
            str(socket_path), service_label="chrome MCP daemon", asleep=asleep
        )

    return connect


class ReconnectingSocketLink(ReconnectingLink):
    def __init__(
        self,
        *,
        socket_path: Path,
        timeout_reader: Callable[[], float],
        max_retries: int = 5,
        base_delay: float = 1.0,
        asleep: Callable[[float], Awaitable[None]] = retry_asleep,
    ) -> None:
        super().__init__(
            _connector(socket_path, asleep=asleep),
            lambda reader, writer: Link(reader, writer, timeout_reader=timeout_reader),
            max_attempts=max_retries + 1,
            base_delay=base_delay,
            rejection_message=_UPSTREAM_DOWN_MSG,
            close_on_error=is_transport_error,
            asleep=asleep,
        )


async def _serve(
    *,
    socket_path: Path,
    timeout_reader: Callable[[], float],
    routes: BrowserRoutes,
    agent_id: int | None,
    caller: str | None = None,
) -> None:
    link = ReconnectingSocketLink(socket_path=socket_path, timeout_reader=timeout_reader)

    async def local_request(payload: dict[str, object]) -> object:
        _ = payload.pop("agent_id", None)
        return await link.request(payload)

    session = RoutedBrowserSession(routes, local_request=local_request)
    session.client_agent_id = agent_id
    session.browser_caller = caller
    await session.start()

    async def _list_tools(
        _ctx: object, _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return await session.list_tools()

    async def _call_tool(_ctx: object, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await session.call_tool(params.name, params.arguments or {})

    server: Server[object] = Server("chrome", on_list_tools=_list_tools, on_call_tool=_call_tool)
    try:
        async with stdio_server() as (server_read, server_write):
            await server.run(server_read, server_write, server.create_initialization_options())
    finally:
        link.close()
        await session.close()


def main() -> None:
    config = ConfigBoot()
    config.boot()
    home = meiva_home()
    raw_id = os.environ.get(AGENT_ID_ENV)
    asyncio.run(
        _serve(
            socket_path=chrome_mcp_socket(home, config.view.services.browser_cdp_port),
            timeout_reader=lambda: config.view.sandbox.mcp_connect_timeout_seconds,
            routes=browser_routes_from_boot(
                config, home=home, timers=SYSTEM_TIMERS, host=Host.system(environ=os.environ)
            ),
            agent_id=int(raw_id) if raw_id else None,
            caller=os.environ.get(BROWSER_CALLER_ENV) or os.environ.get(HANDLE_ENV),
        )
    )


if __name__ == "__main__":
    main()
