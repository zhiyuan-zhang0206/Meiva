# meiva:link agent/docs/mcp-daemon.md

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from mcp.types import CallToolResult, ListToolsResult


class BrowserSession(Protocol):
    client_agent_id: int | None
    browser_caller: str | None

    async def start(self) -> None: ...
    async def list_tools(self) -> ListToolsResult: ...
    async def call_tool(self, name: str, args: dict[str, object]) -> CallToolResult: ...
    async def close(self) -> None: ...


type BrowserRequest = Callable[[dict[str, object]], Awaitable[object]]
type BrowserFactory = Callable[[BrowserRequest], BrowserSession]
