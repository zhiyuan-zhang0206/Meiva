# meiva:note
# Shared fixtures and builders for the meiva.mcps test files. Tool sources run over a real stub daemon socket (`stub_daemon.py`); the wire builders below shape what that daemon answers.
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.agents.context.identity import AgentIdentity
from meiva.mcps._clients import McpClients
from meiva.mcps._remote import RemoteMCPClient
from meiva.mcps._tools import RemoteClient, ToolCache, ToolSource
from meiva.mcps.config import ToolInfo
from meiva.mcps.tests.stub_daemon import Respond, StubDaemon, ok_line


# meiva:note
# This test's own MCP clients (a daemon client per test) for the daemon socket at `socket_path`.
def local_mcp_clients(socket_path: Path, timeout_seconds: float = 15.0) -> McpClients:
    return McpClients(socket_path, lambda: timeout_seconds)


class NoCache:
    """A tool cache that keeps nothing: every read misses, every write is dropped."""

    def read(self, server: str) -> list[ToolInfo] | None:
        return None

    def write(self, server: str, tools: list[ToolInfo]) -> None:
        return None


class StoredTools:
    """A tool cache that always holds `tools` and counts its reads."""

    def __init__(self, tools: list[ToolInfo]) -> None:
        self.tools = tools
        self.reads = 0

    def read(self, server: str) -> list[ToolInfo] | None:
        self.reads += 1
        return self.tools

    def write(self, server: str, tools: list[ToolInfo]) -> None:
        return None


# meiva:note
# A source over the given daemon client: no cache unless one is passed, no identity unless `identity` is.
def tool_source(
    remote: RemoteClient,
    *,
    timeout_seconds: float = 5.0,
    cache: ToolCache | None = None,
    identity: AgentIdentity | None = None,
) -> ToolSource:
    return ToolSource(
        remote=remote,
        timeout_seconds=timeout_seconds,
        cache=cache if cache is not None else NoCache(),
        identity=identity,
    )


# meiva:note
# The machine-level `mcp.json` of this test's own home; nothing is written to it yet.
@pytest.fixture
def machine_config(unit_home: Path) -> Path:
    return unit_home / "mcp.json"


# meiva:note
# One tool as the daemon lists it.
def tool_wire(name: str, description: str = "", schema: dict[str, Any] | None = None) -> ToolInfo:
    return {"name": name, "description": description, "input_schema": schema or {}}


# meiva:note
# A tool result of text blocks, as the daemon sends it.
def text_result(
    *texts: str, is_error: bool = False, structured: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text} for text in texts],
        "isError": is_error,
        "structuredContent": structured,
    }


# meiva:note
# A tool result with an image block after any text blocks.
def image_result(b64: str, *texts: str, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    result = text_result(*texts, structured=structured)
    result["content"].append({"type": "image", "data": b64, "mimeType": "image/png"})
    return result


# meiva:note
# A stub daemon listing `tools` and answering each `call_tool` with `results[tool]`, with the production client dialing it.
def canned_daemon(
    start: Callable[[Respond], StubDaemon],
    *,
    tools: list[ToolInfo] | None = None,
    results: dict[str, dict[str, Any]] | None = None,
) -> tuple[StubDaemon, RemoteMCPClient]:
    listed = tools or []
    answers = results or {}

    def respond(request: dict[str, Any]) -> list[bytes]:
        if request["method"] == "list_tools":
            return [ok_line(request, listed)]
        return [ok_line(request, answers[request["params"]["tool"]])]

    daemon = start(respond)
    return daemon, RemoteMCPClient(daemon.path)
