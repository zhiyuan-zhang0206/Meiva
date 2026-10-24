# meiva:note
# A real streamable-http MCP server for the daemon tests, served in the running event loop on a port the kernel picks (port 0). It offers one tool, `echo`, and records the headers of every request it receives, so a test sees what a client really sent.
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer


@dataclass
class HttpMcp:
    url: str
    seen_headers: list[dict[str, str]] = field(default_factory=list[dict[str, str]])


class _Recorder:
    def __init__(self, app: Any, sink: list[dict[str, str]]) -> None:
        self._app = app
        self._sink = sink

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self._sink.append({k.decode(): v.decode() for k, v in scope["headers"]})
        await self._app(scope, receive, send)


@asynccontextmanager
async def http_mcp_server() -> AsyncGenerator[HttpMcp]:
    import asyncio

    server = MCPServer("http-fixture")

    @server.tool()
    def echo(text: str) -> str:
        return text

    app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="")
    seen: list[dict[str, str]] = []
    config = uvicorn.Config(
        _Recorder(app, seen), host="127.0.0.1", port=0, log_config=None, lifespan="on"
    )
    serving = uvicorn.Server(config)
    task = asyncio.create_task(serving.serve())
    while not serving.started:
        await asyncio.sleep(0.01)
    port = serving.servers[0].sockets[0].getsockname()[1]
    try:
        yield HttpMcp(f"http://127.0.0.1:{port}/mcp", seen)
    finally:
        serving.should_exit = True
        await task
