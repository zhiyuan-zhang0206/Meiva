"""One lazy MCP daemon socket client per MeivaContext."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from meiva.mcps.config import MCPConnectError, ToolInfo

from ._remote import RemoteMCPClient, connect_remote


# meiva:note
# The context owns the daemon connection and the request timeout its composition root read. `socket_path` is where the machine's MCP daemon listens (`base.paths.mcp_daemon_shared_socket` of the home), given by the composition root; the socket is looked for when the first request dials, since the daemon may start after the context.
class McpClients:
    def __init__(self, socket_path: Path, timeout_seconds: float) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._remote: RemoteMCPClient | None = None

    def remote(self) -> RemoteMCPClient:
        """Require a daemon socket; never spawn an alternative tool runtime."""
        if not self.socket_path.exists():
            raise MCPConnectError("MCP daemon unavailable; start the machine's mcp-daemon service")
        with self._lock:
            if self._remote is None:
                self._remote = connect_remote(str(self.socket_path))
            return self._remote

    # meiva:note
    # The daemon calls the tools make (`meiva.mcps._tools.ToolSource.remote`): each dials the daemon on its first request, so a context that never calls an MCP tool holds no socket.
    def list_tools(
        self, server: str, *, timeout_seconds: float, agent_id: int | None
    ) -> list[ToolInfo]:
        return self.remote().list_tools(server, timeout_seconds=timeout_seconds, agent_id=agent_id)

    def call_tool(
        self,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        timeout_seconds: float,
        agent_id: int | None,
    ) -> dict[str, Any]:
        return self.remote().call_tool(
            server, tool, args, timeout_seconds=timeout_seconds, agent_id=agent_id
        )

    def close(self) -> None:
        """Drop this context's daemon connection."""
        with self._lock:
            remote, self._remote = self._remote, None
        if remote is not None:
            remote.close()
