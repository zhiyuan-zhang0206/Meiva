# meiva:note
# A real streamable-http MCP server for the daemon tests, served in the running event loop on a port the kernel picks (port 0). It offers one tool, `echo`, and records the headers of every request it receives, so a test sees what a client really sent.
from __future__ import annotations

import signal
import threading
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
    main_thread = threading.current_thread() is threading.main_thread()
    interrupts = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    task = asyncio.create_task(serving.serve())
    while not serving.started:
        await asyncio.sleep(0.01)
    port = serving.servers[0].sockets[0].getsockname()[1]
    # meiva:note
    # This server keeps no shutdown duty towards the process: uvicorn installs its own SIGTERM and
    # SIGINT handler while serving, and sse_starlette's per-loop watcher reads the bound server from
    # that handler to learn the process is going down. It then sets one process-global flag that ends
    # every SSE response, so a test server shutting down used to kill the *next* server's response
    # mid-stream ("SSE stream ended without a response" on the client, "ASGI callable returned
    # without completing response" in uvicorn). Handing the previous handlers back leaves the
    # process to pytest and keeps each in-process server's streams tied to its own lifetime.
    # meiva:link meiva/mcps/_daemon.py
    if main_thread:
        for interrupt, handler in interrupts.items():
            if signal.getsignal(interrupt) is not handler:
                signal.signal(interrupt, handler)
    try:
        yield HttpMcp(f"http://127.0.0.1:{port}/mcp", seen)
    finally:
        serving.should_exit = True
        await task
