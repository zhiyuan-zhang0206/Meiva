# meiva:note
# meiva.mcps remote client and proxy detail: the Unix-socket client against a real stub daemon (`stub_daemon.py`), the display-only tool signatures, and the guarantee that a missing daemon never starts a local server. Split from meiva/mcps/tests/test_mcps.py (task #4922).
import inspect
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import meiva.mcps as mcps_mod
import meiva.mcps._remote as remote_mod
from meiva.mcps._tools import ServerProxy, call_raw, schema_to_signature
from meiva.mcps.tests._mcps_helpers import (
    StoredTools,
    canned_daemon,
    local_mcp_clients,
    tool_source,
    tool_wire,
)
from meiva.mcps.tests._mcps_helpers import machine_config as machine_config
from meiva.mcps.tests.stub_daemon import (
    CLOSE,
    Respond,
    StubDaemon,
    error_line,
    line,
    ok_line,
)
from meiva.mcps.tests.stub_daemon import stub_daemon as stub_daemon

Start = Callable[[Respond], StubDaemon]


def answering(result: object) -> Respond:
    return lambda request: [ok_line(request, result)]


# meiva:note
# The client keeps no timeout of its own: a request whose budget is shorter than the reply delay
# fails, and the next request with a longer budget succeeds on the same client.
def test_each_request_waits_with_its_own_timeout(stub_daemon: Start) -> None:
    daemon = stub_daemon(lambda request: [0.5, ok_line(request, [])])
    client = remote_mod.RemoteMCPClient(daemon.path)

    with pytest.raises(mcps_mod.MCPConnectError, match="timeout"):
        client.list_tools("srv", timeout_seconds=0.1, agent_id=None)
    assert client.list_tools("srv", timeout_seconds=5.0, agent_id=None) == []


# meiva:note
# A stream of foreign-id lines never extends the budget: the matching line never arrives, and the
# request ends with the timeout rather than with the stream's end.
def test_foreign_responses_do_not_reset_the_request_deadline(stub_daemon: Start) -> None:
    def foreign_stream(request: dict[str, Any]) -> list[bytes | float]:
        steps: list[bytes | float] = []
        for i in range(60):
            steps += [line({"id": 9000 + i, "ok": True, "result": []}), 0.05]
        return steps

    client = remote_mod.RemoteMCPClient(stub_daemon(foreign_stream).path)
    with pytest.raises(mcps_mod.MCPConnectError, match="timeout"):
        client.list_tools("srv", timeout_seconds=0.3, agent_id=None)


def test_list_tools_roundtrip(stub_daemon: Start) -> None:
    daemon = stub_daemon(
        answering([{"name": "t1", "description": "d1", "input_schema": {"type": "object"}}])
    )
    client = remote_mod.RemoteMCPClient(daemon.path)

    tools = client.list_tools("srv", timeout_seconds=5.0, agent_id=7)

    assert tools == [{"name": "t1", "description": "d1", "input_schema": {"type": "object"}}]
    [request] = daemon.requests
    assert request == {"id": 1, "method": "list_tools", "params": {"server": "srv"}, "agent_id": 7}


def test_call_tool_roundtrip(stub_daemon: Start) -> None:
    result: dict[str, Any] = {"content": [], "isError": False, "structuredContent": None}
    daemon = stub_daemon(answering(result))
    client = remote_mod.RemoteMCPClient(daemon.path)

    out = client.call_tool("srv", "tool_x", {"k": "v"}, timeout_seconds=5.0, agent_id=None)

    assert out == result
    [request] = daemon.requests
    assert request["method"] == "call_tool"
    assert request["params"] == {"server": "srv", "tool": "tool_x", "args": {"k": "v"}}
    assert request["agent_id"] is None


# meiva:note
# `ok: False` is a cleanly-consumed response: MCPCallError with the error text, and the stream stays
# aligned, so the next request reuses the connection.
def test_error_response_raises_and_keeps_the_connection(stub_daemon: Start) -> None:
    replies = iter([False, True])

    def respond(request: dict[str, Any]) -> list[bytes]:
        if next(replies):
            return [ok_line(request, [])]
        return [error_line(request, "permission denied")]

    daemon = stub_daemon(respond)
    client = remote_mod.RemoteMCPClient(daemon.path)
    with pytest.raises(mcps_mod.MCPCallError, match="permission denied"):
        client.list_tools("srv", timeout_seconds=5.0, agent_id=None)
    assert client.list_tools("srv", timeout_seconds=5.0, agent_id=None) == []
    assert daemon.connections == 1


# meiva:note
# EOF mid-request leaves the stream ambiguous: the socket is dropped, so the next request dials a
# fresh connection.
def test_connection_closed_raises_and_the_next_request_reconnects(stub_daemon: Start) -> None:
    closes = iter([True, False])

    def respond(request: dict[str, Any]) -> list[object]:
        return [CLOSE] if next(closes) else [ok_line(request, [])]

    daemon = stub_daemon(respond)
    client = remote_mod.RemoteMCPClient(daemon.path)
    with pytest.raises(mcps_mod.MCPConnectError, match="connection closed"):
        client.list_tools("srv", timeout_seconds=5.0, agent_id=None)
    assert client.list_tools("srv", timeout_seconds=5.0, agent_id=None) == []
    assert daemon.connections == 2


# meiva:note
# A request-timeout leaves the stream ambiguous: the daemon may still be processing and its response
# may arrive later. Closing the socket means the next request reconnects fresh, and a late response
# can never be consumed by it (the cross-talk enabler).
def test_timeout_drops_the_socket_so_a_late_reply_cannot_reach_the_next_request(
    stub_daemon: Start,
) -> None:
    delays = iter([0.4, 0.0])

    def respond(request: dict[str, Any]) -> list[bytes | float]:
        return [next(delays), ok_line(request, [{"name": f"late-{request['id']}"}])]

    daemon = stub_daemon(respond)
    client = remote_mod.RemoteMCPClient(daemon.path)
    with pytest.raises(mcps_mod.MCPConnectError, match="timeout"):
        client.list_tools("srv", timeout_seconds=0.1, agent_id=None)
    tools = client.list_tools("srv", timeout_seconds=5.0, agent_id=None)
    assert [t["name"] for t in tools] == ["late-2"]
    assert daemon.connections == 2


# meiva:note
# The response arrives in two parts (TCP-style): concatenated until the newline, then parsed.
def test_chunked_response_is_accumulated_until_the_newline(stub_daemon: Start) -> None:
    payload = line({"id": 1, "ok": True, "result": []})
    daemon = stub_daemon(lambda _request: [payload[:10], 0.05, payload[10:]])
    client = remote_mod.RemoteMCPClient(daemon.path)
    assert client.list_tools("srv", timeout_seconds=5.0, agent_id=None) == []


def test_request_ids_increment(stub_daemon: Start) -> None:
    daemon = stub_daemon(answering([]))
    client = remote_mod.RemoteMCPClient(daemon.path)
    client.list_tools("srv", timeout_seconds=5.0, agent_id=None)
    client.list_tools("srv", timeout_seconds=5.0, agent_id=None)
    assert [request["id"] for request in daemon.requests] == [1, 2]


# meiva:note
# Only the line whose id equals this request's id is its response: consuming a foreign line would
# shift every later response by one (response cross-talk).
@pytest.mark.parametrize(
    "foreign",
    [
        # meiva:note
        # A stale answer to a request whose client-side deadline already fired.
        line({"id": 0, "ok": True, "result": "stale"}),
        # meiva:note
        # An unsolicited notification carries no id.
        line({"method": "notifications/message", "params": {}}),
    ],
    ids=["stale-response", "notification"],
)
def test_foreign_lines_before_the_response_are_skipped(stub_daemon: Start, foreign: bytes) -> None:
    daemon = stub_daemon(lambda request: [foreign, ok_line(request, "fresh")])
    client = remote_mod.RemoteMCPClient(daemon.path)
    assert client.call_tool("srv", "t", {}, timeout_seconds=5.0, agent_id=None) == "fresh"


# meiva:note
# Both foreign-line classes in one burst before the matching response.
def test_stale_response_and_notification_together_are_skipped(stub_daemon: Start) -> None:
    daemon = stub_daemon(
        lambda request: [
            line({"id": 7, "ok": True, "result": "very stale"})
            + line({"method": "notifications/progress", "params": {}})
            + ok_line(request, "mine")
        ]
    )
    client = remote_mod.RemoteMCPClient(daemon.path)
    assert client.call_tool("srv", "t", {}, timeout_seconds=5.0, agent_id=None) == "mine"


# meiva:note
# A truncated / garbage line is a stream-integrity failure: MCPConnectError (discovery may use
# cached metadata) and the socket is closed for a fresh reconnect.
def test_malformed_response_line_raises_and_the_next_request_reconnects(
    stub_daemon: Start,
) -> None:
    garbage = iter([True, False])

    def respond(request: dict[str, Any]) -> list[bytes]:
        return [b"not json at all\n"] if next(garbage) else [ok_line(request, [])]

    daemon = stub_daemon(respond)
    client = remote_mod.RemoteMCPClient(daemon.path)
    with pytest.raises(mcps_mod.MCPConnectError, match="malformed"):
        client.list_tools("srv", timeout_seconds=5.0, agent_id=None)
    assert client.list_tools("srv", timeout_seconds=5.0, agent_id=None) == []
    assert daemon.connections == 2


# meiva:note
# After a call is sent, a lost reply is UNKNOWN: it is never replayed.
def test_a_failed_tool_call_reports_an_unknown_result(stub_daemon: Start) -> None:
    daemon = stub_daemon(lambda _request: [CLOSE])
    client = remote_mod.RemoteMCPClient(daemon.path)
    with pytest.raises(mcps_mod.MCPCallError, match="result unknown"):
        client.call_tool("srv", "charge", {}, timeout_seconds=5.0, agent_id=None)
    assert len(daemon.requests) == 1


def test_proxy_getattr_rejects_underscore_names(stub_daemon: Start) -> None:
    """`proxy._anything` (dunder / private) goes through AttributeError — not treated as tool call."""
    _, remote = canned_daemon(stub_daemon)
    proxy = ServerProxy("fs", tool_source(remote))
    with pytest.raises(AttributeError):
        proxy._not_a_tool  # noqa: B018 — intentionally trigger __getattr__


def test_proxy_tool_docstring_includes_schema(stub_daemon: Start) -> None:
    """tool has input_schema → docstring includes JSON schema (for agent to see)."""
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    _, remote = canned_daemon(stub_daemon, tools=[tool_wire("read_file", "Read a file", schema)])
    doc = ServerProxy("fs", tool_source(remote)).read_file.__doc__ or ""
    assert "Input schema" in doc
    assert "path" in doc
    assert "string" in doc


def test_proxy_all_prefers_disk_cache(tmp_path: Path) -> None:
    """`__all_for_meiva__` hits the cache then stops - it must not ask the daemon (server startup is
    expensive)."""
    cached: list[mcps_mod.ToolInfo] = [
        {"name": "alpha", "description": "", "input_schema": {}},
        {"name": "beta", "description": "", "input_schema": {}},
    ]
    # meiva:note
    # No daemon listens here: asking it would fail instead of answering from the cache.
    unreachable = remote_mod.RemoteMCPClient(str(tmp_path / "absent.sock"))
    proxy = ServerProxy("fs", tool_source(unreachable, cache=StoredTools(cached)))
    assert proxy.__all_for_meiva__ == ["alpha", "beta"]


def test_proxy_load_tool_names_uses_in_memory_cache_on_second_call(tmp_path: Path) -> None:
    """Second call to _load_tool_names -> reads the proxy's own tool table without the cache again."""
    cache = StoredTools([{"name": "t1", "description": "", "input_schema": {}}])
    unreachable = remote_mod.RemoteMCPClient(str(tmp_path / "absent.sock"))
    proxy = ServerProxy("fs", tool_source(unreachable, cache=cache))
    proxy._load_tool_names()  # first: the cache fills the proxy's table
    proxy._load_tool_names()  # second: uses the in-memory table
    assert cache.reads == 1  # cache read only once


def test_schema_to_signature_renders_required_and_optional() -> None:
    """Has properties → synthesizes keyword-only signature: required no default, optional default None,
    returns str. Purely display, no validation."""
    schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "timeout": {"type": "integer"}},
        "required": ["url"],
    }
    sig = schema_to_signature(schema)
    assert sig is not None
    params = sig.parameters
    assert list(params) == ["url", "timeout"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())
    assert params["url"].default is inspect.Parameter.empty
    assert params["url"].annotation is str
    assert params["timeout"].default is None
    assert params["timeout"].annotation is int
    assert sig.return_annotation is str


def test_schema_to_signature_none_without_properties() -> None:
    """Empty schema / no properties / properties empty dict → None (caller retains (**kwargs))."""
    assert schema_to_signature({}) is None
    assert schema_to_signature({"type": "object"}) is None
    assert schema_to_signature({"properties": {}}) is None


def test_schema_to_signature_none_for_non_identifier_name() -> None:
    """Parameter name is not a valid identifier (hyphen / starts with digit) → overall fallback to (**kwargs)."""
    assert schema_to_signature({"properties": {"page-size": {"type": "integer"}}}) is None


def test_schema_to_signature_none_for_python_keyword_name() -> None:
    """Parameter name collides with Python keyword (`from`) → fallback (.isidentifier() returns True for keywords)."""
    assert schema_to_signature({"properties": {"from": {"type": "string"}}}) is None


def test_schema_to_signature_unknown_type_falls_back_to_any() -> None:
    """type is a list (`["string","null"]`) / omitted / non-primitive name → annotation falls back to Any, no crash."""
    schema: dict[str, Any] = {
        "properties": {"a": {"type": ["string", "null"]}, "b": {}, "c": {"type": "geo"}},
        "required": [],
    }
    sig = schema_to_signature(schema)
    assert sig is not None
    assert all(p.annotation is Any for p in sig.parameters.values())


def test_make_tool_callable_attaches_signature(stub_daemon: Start) -> None:
    """proxy.<tool> callable carries signature synthesized from schema → inspect.signature sees real names."""
    schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
    _, remote = canned_daemon(stub_daemon, tools=[tool_wire("navigate", "Go to a URL", schema)])
    sig = inspect.signature(ServerProxy("chrome", tool_source(remote)).navigate)
    assert list(sig.parameters) == ["url"]
    assert sig.parameters["url"].annotation is str


def test_unknown_tool_keeps_kwargs_signature(stub_daemon: Start) -> None:
    """No schema (info=None) → no signature attached, retains (**kwargs)."""
    _, remote = canned_daemon(stub_daemon)
    params = inspect.signature(ServerProxy("chrome", tool_source(remote)).whatever).parameters
    assert list(params) == ["kwargs"]
    assert params["kwargs"].kind is inspect.Parameter.VAR_KEYWORD


def test_tool_signature_shows_real_params_for_mcp_tool(stub_daemon: Start) -> None:
    """An MCP tool callable carries its real parameter names, not (**kwargs: Any)."""
    schema = {
        "properties": {"url": {"type": "string"}, "timeout": {"type": "integer"}},
        "required": ["url"],
    }
    _, remote = canned_daemon(stub_daemon, tools=[tool_wire("navigate", schema=schema)])
    rendered = str(inspect.signature(ServerProxy("chrome", tool_source(remote)).navigate))
    assert "url: str" in rendered
    assert "timeout: int" in rendered
    assert "**kwargs" not in rendered


def test_missing_daemon_never_starts_a_local_server(machine_config: Path, tmp_path: Path) -> None:
    marker = tmp_path / "local-server-started"
    machine_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "effect": {
                        "command": sys.executable,
                        "args": ["-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                    }
                }
            }
        )
    )
    clients = local_mcp_clients(tmp_path / "absent.sock", 0.1)
    try:
        with pytest.raises(mcps_mod.MCPConnectError, match="MCP daemon unavailable"):
            call_raw(tool_source(clients, timeout_seconds=0.1), "effect", "run")
        assert not marker.exists()
    finally:
        clients.close()
