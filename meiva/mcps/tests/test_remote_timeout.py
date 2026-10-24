from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from meiva.mcps._remote import RemoteMCPClient
from meiva.mcps._tools import call_raw, list_tools
from meiva.mcps.config import MCPCallError, ToolInfo
from meiva.mcps.tests._mcps_helpers import StoredTools, tool_source
from meiva.mcps.tests.stub_daemon import Respond, StubDaemon, ok_line
from meiva.mcps.tests.stub_daemon import stub_daemon as stub_daemon

Start = Callable[[Respond], StubDaemon]


def slow(delay_s: float, result: object) -> Respond:
    return lambda request: [delay_s, ok_line(request, result)]


# meiva:note
# The source's timeout is the budget of each request. A listing that outlasts it falls back to the cached metadata; a started tool call that outlasts it is an unknown result, never replayed.
def test_a_listing_that_outlasts_the_source_timeout_falls_back_to_the_cache(
    stub_daemon: Start,
) -> None:
    cached: list[ToolInfo] = [{"name": "cached_tool", "description": "", "input_schema": {}}]
    client = RemoteMCPClient(stub_daemon(slow(0.5, [])).path)
    source = tool_source(client, timeout_seconds=0.1, cache=StoredTools(cached))
    assert list_tools(source, "srv") == cached


def test_a_listing_inside_the_source_timeout_is_used(stub_daemon: Start) -> None:
    tools: list[ToolInfo] = [{"name": "t1", "description": "d", "input_schema": {}}]
    cached: list[ToolInfo] = [{"name": "cached_tool", "description": "", "input_schema": {}}]
    client = RemoteMCPClient(stub_daemon(slow(0.05, tools)).path)
    source = tool_source(client, timeout_seconds=5.0, cache=StoredTools(cached))
    assert list_tools(source, "srv") == tools


def test_a_call_that_outlasts_the_source_timeout_is_an_unknown_result(stub_daemon: Start) -> None:
    daemon = stub_daemon(slow(0.5, {"content": [], "isError": False}))
    source = tool_source(RemoteMCPClient(daemon.path), timeout_seconds=0.1)
    with pytest.raises(MCPCallError, match="result unknown"):
        call_raw(source, "srv", "tool_x", arg="v")
    assert len(daemon.requests) == 1


def test_a_larger_timeout_lets_the_same_call_finish(stub_daemon: Start) -> None:
    result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
    client = RemoteMCPClient(stub_daemon(slow(0.2, result)).path)
    assert call_raw(tool_source(client, timeout_seconds=5.0), "srv", "tool_x") == result


def test_a_missing_daemon_socket_is_a_transport_error_not_a_hang(tmp_path: Path) -> None:
    client = RemoteMCPClient(str(tmp_path / "absent.sock"))
    with pytest.raises(OSError):
        list_tools(tool_source(client, timeout_seconds=0.1), "srv")
