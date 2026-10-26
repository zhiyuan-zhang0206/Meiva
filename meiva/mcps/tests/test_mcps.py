# meiva:note
# meiva.mcps unit tests: config parsing, namespace dispatch, tool invocation and the disk cache. Tool calls run over a real stub daemon socket (`stub_daemon.py`); the machine config lives in the test's own home, next to whatever the repo bundles.
import json
from collections.abc import Callable
from pathlib import Path

import pytest

import meiva.mcps as mcps_mod
import meiva.mcps._remote as remote_mod
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from meiva.mcps._clients import McpClients
from meiva.mcps._tools import DiskCache, ServerProxy, cached_tools, call_raw, list_tools
from meiva.mcps.tests._mcps_helpers import (
    canned_daemon,
    image_result,
    local_mcp_clients,
    stored_tools,
    text_result,
    tool_source,
    tool_wire,
)
from meiva.mcps.tests._mcps_helpers import machine_config as machine_config
from meiva.mcps.tests.stub_daemon import Respond, StubDaemon, error_line
from meiva.mcps.tests.stub_daemon import stub_daemon as stub_daemon
from meiva.sdk_surface.tests.agent_binding import BindAgent

Start = Callable[[Respond], StubDaemon]


def _clock() -> Clock:
    timers = ManualTimers()
    return Clock(
        ClockConfig("UTC", None, message_timestamp_weekday=False), timers=timers, now=timers.now
    )


# meiva:note
# The servers that exist before a test writes any machine config: the ones the repo bundles.
@pytest.fixture
def builtin_servers(machine_config: Path) -> list[str]:
    return mcps_mod.servers()


def write_servers(path: Path, servers: dict[str, dict[str, str]]) -> None:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_servers_are_only_the_bundled_ones_without_machine_config(
    builtin_servers: list[str],
) -> None:
    assert mcps_mod.servers() == builtin_servers
    assert "fs" not in builtin_servers


def test_servers_lists_configured_names_sorted(
    machine_config: Path, builtin_servers: list[str]
) -> None:
    write_servers(
        machine_config,
        {"zebra": {"command": "z"}, "apple": {"command": "a"}, "mango": {"command": "m"}},
    )
    assert mcps_mod.servers() == sorted([*builtin_servers, "apple", "mango", "zebra"])


# meiva:note
# A file without an mcpServers section adds nothing (compatible with Claude Code generic settings).
def test_servers_ignore_a_file_without_the_section(
    machine_config: Path, builtin_servers: list[str]
) -> None:
    machine_config.write_text(json.dumps({"other_key": {}}), encoding="utf-8")
    assert mcps_mod.servers() == builtin_servers


def test_load_config_raises_on_bad_json(machine_config: Path) -> None:
    machine_config.write_text("not json {{{", encoding="utf-8")
    with pytest.raises(mcps_mod.MCPError, match="Failed to read"):
        mcps_mod.load_config()


def test_load_config_raises_when_section_not_dict(machine_config: Path) -> None:
    """`mcpServers` is not a dict (typo / old format) → MCPError immediately, not silently go empty."""
    machine_config.write_text(json.dumps({"mcpServers": [1, 2, 3]}), encoding="utf-8")
    with pytest.raises(mcps_mod.MCPError, match="mcpServers field is not a dict"):
        mcps_mod.load_config()


def test_description_returns_config_field(machine_config: Path) -> None:
    write_servers(machine_config, {"notes": {"command": "x", "description": "drive a notebook"}})
    assert mcps_mod.description("notes") == "drive a notebook"


def test_description_none_when_field_absent(machine_config: Path) -> None:
    # No `description` field -> the capabilities index renders a bare name.
    write_servers(machine_config, {"notes": {"command": "x"}})
    assert mcps_mod.description("notes") is None


def test_description_raises_for_missing_server(machine_config: Path) -> None:
    write_servers(machine_config, {"fs": {"command": "x"}})
    with pytest.raises(mcps_mod.MCPServerNotFound, match="nope"):
        mcps_mod.description("nope")


# ─── module-level __getattr__ / __dir__ namespace dispatch ────────────────


def test_module_getattr_returns_proxy(machine_config: Path, bound_agent: BindAgent) -> None:
    bound_agent(
        1,
        clients=ClientSet(factories={McpClients: lambda: McpClients(Path("unused.sock"), 1.0)}),
        clock_factory=_clock,
    )
    write_servers(machine_config, {"fs": {"command": "x"}})
    proxy = mcps_mod.fs  # goes through module-level __getattr__
    assert isinstance(proxy, ServerProxy)
    assert proxy._server == "fs"


# meiva:note
# The facade reads the bound context once: its daemon client, the timeout in force, the disk cache under the home, and the context's agent identity.
def test_a_proxy_runs_against_the_bound_contexts_values(
    unit_home: Path, machine_config: Path, bound_agent: BindAgent
) -> None:
    clients = ClientSet(factories={McpClients: lambda: McpClients(Path("unused.sock"), 7.5)})
    bound_agent(3, clients=clients, clock_factory=_clock)
    write_servers(machine_config, {"fs": {"command": "x"}})
    source = mcps_mod.fs._source
    assert source.now == _clock().now().timestamp()
    assert source.remote is clients.get(McpClients)
    assert source.timeout_seconds == 7.5
    assert source.cache == DiskCache(unit_home / "mcp_cache")
    assert source.identity == AgentIdentity(agent_id=3, owns_loop=True)


def test_module_getattr_raises_for_missing_server(machine_config: Path) -> None:
    write_servers(machine_config, {"fs": {"command": "x"}})
    with pytest.raises(AttributeError, match="nope"):
        mcps_mod.nope  # noqa: B018 — intentionally trigger __getattr__


def test_module_dir_lists_servers(machine_config: Path) -> None:
    write_servers(machine_config, {"fs": {"command": "x"}, "github": {"command": "y"}})
    listing = dir(mcps_mod)
    # server names are present; tool methods are present; other _-prefixed filtered out
    assert "fs" in listing
    assert "github" in listing
    assert "servers" in listing
    assert "description" in listing


def test_the_agent_visible_surface_is_the_helpers_plus_the_configured_servers(
    machine_config: Path, builtin_servers: list[str]
) -> None:
    """`__all_for_meiva__` is declared on the module class and computed live, so `help()` and the
    metering wrappers see exactly `servers` / `description` and each configured server, however
    many names the module itself gains."""
    assert mcps_mod.__all_for_meiva__ == sorted([*builtin_servers, "description", "servers"])
    write_servers(machine_config, {"fs": {"command": "x"}, "github": {"command": "y"}})
    assert mcps_mod.__all_for_meiva__ == sorted(
        [*builtin_servers, "description", "fs", "github", "servers"]
    )


# ─── tools / call / call_raw via ServerProxy ─────────────────────────────


def test_proxy_dir_lists_tools_plus_raw(stub_daemon: Start) -> None:
    """`dir(proxy)` goes through list_tools to get tool names + `raw` is also present (visible in meiva.help)."""
    _, remote = canned_daemon(stub_daemon, tools=[tool_wire("read_file"), tool_wire("write_file")])
    proxy = ServerProxy("fs", tool_source(remote))
    assert dir(proxy) == ["raw", "read_file", "write_file"]


# meiva:note
# `proxy.read_file` returns a callable; calling it sends one `call_tool` request to the daemon.
def test_proxy_attribute_returns_callable_that_calls_the_daemon(stub_daemon: Start) -> None:
    daemon, remote = canned_daemon(
        stub_daemon,
        tools=[tool_wire("read_file", "Read a file")],
        results={"read_file": text_result("file contents")},
    )
    proxy = ServerProxy("fs", tool_source(remote))
    fn = proxy.read_file
    assert callable(fn)
    assert fn.__name__ == "read_file"
    assert "Read a file" in (fn.__doc__ or "")
    assert fn(path="/x") == "file contents"
    calls = [r["params"] for r in daemon.requests if r["method"] == "call_tool"]
    assert calls == [{"server": "fs", "tool": "read_file", "args": {"path": "/x"}}]


def test_proxy_attribute_works_for_unknown_tool_too(stub_daemon: Start) -> None:
    """Tool names not in cache also return callable — MCP server side will reject wrong names,
    error message is more reliable."""
    _, remote = canned_daemon(stub_daemon, results={"unknown_tool": text_result("ok")})
    proxy = ServerProxy("fs", tool_source(remote))
    fn = proxy.unknown_tool
    assert callable(fn)
    assert fn() == "ok"


def test_proxy_call_joins_text_blocks(stub_daemon: Start) -> None:
    _, remote = canned_daemon(
        stub_daemon, tools=[tool_wire("do")], results={"do": text_result("line 1", "line 2")}
    )
    assert ServerProxy("fs", tool_source(remote)).do() == "line 1\nline 2"


def test_proxy_call_raises_on_is_error(stub_daemon: Start) -> None:
    _, remote = canned_daemon(
        stub_daemon,
        tools=[tool_wire("do")],
        results={"do": text_result("permission denied", is_error=True)},
    )
    with pytest.raises(mcps_mod.MCPCallError, match="permission denied"):
        ServerProxy("fs", tool_source(remote)).do()


def test_proxy_call_raises_on_non_text_content(stub_daemon: Start) -> None:
    """tool returned image / other non-text content → guides to use .raw()."""
    _, remote = canned_daemon(
        stub_daemon, tools=[tool_wire("screenshot")], results={"screenshot": image_result("b64==")}
    )
    with pytest.raises(mcps_mod.MCPCallError, match=r"\.raw"):
        ServerProxy("fs", tool_source(remote)).screenshot()


def test_proxy_call_empty_content_returns_empty_string(stub_daemon: Start) -> None:
    _, remote = canned_daemon(
        stub_daemon, tools=[tool_wire("noop")], results={"noop": text_result()}
    )
    assert ServerProxy("fs", tool_source(remote)).noop() == ""


def test_proxy_call_empty_text_block_returns_empty_string(stub_daemon: Start) -> None:
    """A response of all-text blocks that join to "" (e.g. chrome list_pages
    with no open pages) is a legitimate "" — not a spurious
    'non-text content (kinds=[text])' error."""
    _, remote = canned_daemon(
        stub_daemon, tools=[tool_wire("list_pages")], results={"list_pages": text_result("")}
    )
    assert ServerProxy("fs", tool_source(remote)).list_pages() == ""


def test_proxy_call_empty_text_with_image_still_raises(stub_daemon: Start) -> None:
    """An empty text block alongside a non-text block still points at .raw() —
    the real payload (image) can't be text-joined."""
    _, remote = canned_daemon(
        stub_daemon, tools=[tool_wire("shot")], results={"shot": image_result("b64==", "")}
    )
    with pytest.raises(mcps_mod.MCPCallError, match=r"\.raw"):
        ServerProxy("fs", tool_source(remote)).shot()


def test_proxy_raw_returns_full_structure(stub_daemon: Start) -> None:
    _, remote = canned_daemon(
        stub_daemon,
        tools=[tool_wire("do")],
        results={"do": image_result("xyz==", "ok", structured={"counted": 42})},
    )
    out = ServerProxy("fs", tool_source(remote)).raw("do", arg1="v")
    assert out["isError"] is False
    assert out["structuredContent"] == {"counted": 42}
    assert out["content"] == [
        {"type": "text", "text": "ok"},
        {"type": "image", "data": "xyz==", "mimeType": "image/png"},
    ]


def test_proxy_raw_does_not_raise_on_is_error(stub_daemon: Start) -> None:
    """raw does not convert isError to raise — returns full structure for caller to decide."""
    _, remote = canned_daemon(
        stub_daemon,
        tools=[tool_wire("x")],
        results={"x": text_result("permission denied", is_error=True)},
    )
    assert ServerProxy("fs", tool_source(remote)).raw("x")["isError"] is True


def test_proxy_call_structured_only_raises(stub_daemon: Start) -> None:
    """Only returned structuredContent, no text → raise (guides to use .raw())."""
    _, remote = canned_daemon(
        stub_daemon,
        tools=[tool_wire("count")],
        results={"count": text_result(structured={"counted": 42})},
    )
    with pytest.raises(mcps_mod.MCPCallError, match="structuredContent"):
        ServerProxy("fs", tool_source(remote)).count()


# ─── disk cache (DiskCache.read / write) ──────────────────────────────


def _disk_cache(tmp_path: Path) -> DiskCache:
    return DiskCache(tmp_path / "mcp_cache")


def _write_cache_file(tmp_path: Path, document: object) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    (cache_dir / "srv.json").write_text(json.dumps(document), encoding="utf-8")


def test_cache_write_then_read_roundtrip(tmp_path: Path) -> None:
    tools: list[mcps_mod.ToolInfo] = [
        {"name": "t1", "description": "d1", "input_schema": {"type": "object"}}
    ]
    _disk_cache(tmp_path).write("srv", tools)
    assert _disk_cache(tmp_path).read("srv") == tools


def test_read_cache_returns_none_when_missing(tmp_path: Path) -> None:
    assert _disk_cache(tmp_path).read("never_written") is None


def test_read_cache_returns_none_on_bad_json(tmp_path: Path) -> None:
    cache_dir = tmp_path / "mcp_cache"
    cache_dir.mkdir()
    (cache_dir / "srv.json").write_text("not json {{{", encoding="utf-8")
    assert _disk_cache(tmp_path).read("srv") is None


def test_read_cache_returns_none_when_expired(tmp_path: Path) -> None:
    """cached_at=0 → age ≈ now → exceeds 24h TTL, considered expired."""
    _write_cache_file(tmp_path, {"tools": [], "cached_at": 0})
    assert _disk_cache(tmp_path).read("srv", now=100000.0) is None


def test_read_cache_returns_none_when_the_file_is_not_an_object(tmp_path: Path) -> None:
    _write_cache_file(tmp_path, [{"name": "t1"}])
    assert _disk_cache(tmp_path).read("srv") is None


def test_read_cache_fills_defaults_for_missing_fields(tmp_path: Path) -> None:
    """tool entry missing description / input_schema → fill defaults with empty str / empty dict."""
    _write_cache_file(tmp_path, {"tools": [{"name": "t1"}], "cached_at": 0.0})
    assert _disk_cache(tmp_path).read("srv") == [
        {"name": "t1", "description": "", "input_schema": {}}
    ]


@pytest.mark.parametrize(
    "document",
    [
        {"tools": []},
        {"tools": [], "cached_at": "yesterday"},
        {"tools": [], "cached_at": 0.0},
        {"tools": "oops", "cached_at": 100.0},
        {"tools": ["bad"], "cached_at": 100.0},
    ],
)
def test_cached_tools_rejects_an_unusable_entry(document: dict[str, object]) -> None:
    assert cached_tools(document, now=100.0 + 86400.0 + 1.0, ttl_s=86400.0) is None


def test_cached_tools_accepts_an_entry_exactly_at_its_ttl() -> None:
    document = {"tools": [{"name": "t1"}], "cached_at": 100.0}
    assert cached_tools(document, now=100.0 + 86400.0, ttl_s=86400.0) == [
        {"name": "t1", "description": "", "input_schema": {}}
    ]


def test_remote_none_without_daemon(tmp_path: Path) -> None:
    with pytest.raises(mcps_mod.MCPConnectError, match="unavailable"):
        local_mcp_clients(tmp_path / "absent.sock").remote()


def test_the_daemon_calls_fail_the_same_way_without_a_daemon(tmp_path: Path) -> None:
    clients = local_mcp_clients(tmp_path / "absent.sock")
    with pytest.raises(mcps_mod.MCPConnectError, match="unavailable"):
        clients.list_tools("fs", timeout_seconds=1.0, agent_id=None)
    with pytest.raises(mcps_mod.MCPConnectError, match="unavailable"):
        clients.call_tool("fs", "t", {}, timeout_seconds=1.0, agent_id=None)


def test_remote_creates_when_daemon_running(tmp_path: Path) -> None:
    sock = tmp_path / "daemon.sock"
    sock.touch()
    client = local_mcp_clients(sock).remote()
    assert isinstance(client, remote_mod.RemoteMCPClient)
    assert client._socket_path == str(sock)


def test_remote_caches_instance(tmp_path: Path) -> None:
    sock = tmp_path / "daemon.sock"
    sock.touch()
    clients = local_mcp_clients(sock)
    assert clients.remote() is clients.remote()


# meiva:note
# The clients dial the socket path they were given on the first request, not when they are made, and the agent id rides on that request.
def test_the_clients_dial_the_given_socket_on_the_first_request(stub_daemon: Start) -> None:
    daemon, _ = canned_daemon(stub_daemon, tools=[tool_wire("t1")])
    clients = local_mcp_clients(Path(daemon.path))
    assert daemon.connections == 0

    tools = clients.list_tools("fs", timeout_seconds=5.0, agent_id=7)

    assert [t["name"] for t in tools] == ["t1"]
    assert daemon.connections == 1
    assert daemon.requests[0]["agent_id"] == 7
    clients.close()


def test_list_tools_propagates_tool_error_from_remote(stub_daemon: Start) -> None:
    # A server-reported tool error must NOT silently fall back to a local re-run.
    daemon = stub_daemon(lambda request: [error_line(request, "tool blew up")])
    client = local_mcp_clients(Path(daemon.path))
    with pytest.raises(mcps_mod.MCPCallError, match="tool blew up"):
        list_tools(tool_source(client), "fs")


def test_list_tools_falls_back_to_cache_when_no_daemon_listens(tmp_path: Path) -> None:
    # meiva:note
    # Neither a missing socket (the connect error) nor a socket nobody listens on (a transport
    # error) stops the listing while the cache holds the server's tools.
    cached: list[mcps_mod.ToolInfo] = [{"name": "t1", "description": "", "input_schema": {}}]
    stale = tmp_path / "stale.sock"
    stale.touch()
    for socket_path in (tmp_path / "absent.sock", stale):
        client = local_mcp_clients(socket_path)
        cache = stored_tools(tmp_path / f"cache-{socket_path.name}", cached)
        assert list_tools(tool_source(client, cache=cache), "fs") == cached


def test_list_tools_without_a_cache_raises_the_connect_or_transport_error(tmp_path: Path) -> None:
    stale = tmp_path / "stale.sock"
    stale.touch()
    with pytest.raises(mcps_mod.MCPConnectError, match="unavailable"):
        list_tools(tool_source(local_mcp_clients(tmp_path / "absent.sock")), "fs")
    with pytest.raises(OSError):
        list_tools(tool_source(local_mcp_clients(stale)), "fs")


def test_call_raw_propagates_tool_error_from_remote(stub_daemon: Start) -> None:
    daemon = stub_daemon(lambda request: [error_line(request, "permission denied")])
    client = local_mcp_clients(Path(daemon.path))
    with pytest.raises(mcps_mod.MCPCallError, match="permission denied"):
        call_raw(tool_source(client), "fs", "do")
