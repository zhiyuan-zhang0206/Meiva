"""MCP daemon — long-running MCP server management process.

One per-machine daemon subprocess (ops roster session "mcp-daemon") manages
the lifecycle of all MCP servers. Agent execute_code subprocesses connect to
the daemon via Unix socket to call tools; sessions are isolated per client
connection so agent A can never reach agent B's MCP state.

Protocol (JSON-line, one request/response per line):
  Request:  {"id": 1, "method": "list_tools", "params": {"server": "chrome"}}
            {"id": 2, "method": "call_tool",  "params": {"server": "chrome", "tool": "navigate", "args": {"url": "https://example.com"}}}
  Response: {"id": 1, "ok": true,  "result": [...]}
            {"id": 1, "ok": false, "error": "message"}

Server subprocess sharing: a server entry may declare `"shared"` in its
.mcp.json to avoid one stdio child per agent connection:

- `"shared": "browser"` / `"shared": "computer_use"` — no child at all: the daemon
  dials the per-machine browser-mcp / computer-mcp service's line protocol
  directly (each connection keeps its own socket, so page affinity / agent
  identity stays per agent). Replaces the 60+MB mcp_wrapper.
- `"shared": true` — one daemon-wide stdio child serves every connection
  (serialized per server), for stateless servers like x.

Launch: python -m meiva.mcps._daemon [socket_path]
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from mcp import ClientSession
from mcp.types import CallToolResult, ListToolsResult

from base.config import settings
from base.host.system.hostport import Host
from base.host.system.probes import display_available
from base.log.sinks import add_sink
from base.paths import computer_mcp_socket, mcp_daemon_shared_socket, meiva_home
from meiva.mcps._guards import reap_stale_daemons, socket_is_live, unlink_own_socket
from meiva.mcps._plan import HttpConnect, StdioConnect, plan_connect, unmet_requirement
from meiva.mcps._policy import DaemonConfig, retry_delay
from meiva.mcps._startup import USAGE, refusal_to_start, socket_for_args
from meiva.mcps._wire import (
    call_response,
    encode_line,
    error_response,
    ping_response,
    tools_response,
)
from meiva.mcps.config import (
    MCPCallError,
    MCPError,
    is_transport_error,
    load_mcp_config,
    server_cwd,
    server_url,
    validate_requirements,
)

from ._browser import BrowserLineSession, connect_browser_direct
from ._computer import ComputerLineSession, connect_computer_direct
from ._oauth import OAUTH_FLOW_TIMEOUT_S

type DirectSession = ClientSession | BrowserLineSession | ComputerLineSession
type ManagedSession = DirectSession | SerialSession

# ─── server subprocess sharing ─────────────────────────────────────────────
#
# A `.mcp.json` entry may declare `"shared"` to stop paying one stdio child
# per agent connection (chrome's wrapper alone is ~63MB RSS per agent):
#
# - `"shared": "browser"` / `"shared": "computer_use"` — the server is fronted by
#   a per-machine service (browser-mcp / computer-mcp); the daemon dials its
#   line protocol directly and spawns no child. Each connection keeps its own
#   socket, so the service's per-connection affinity still isolates agents (a
#   single shared wrapper would collapse every agent onto one context).
# - `"shared": true` — one daemon-wide stdio child serves every connection,
#   serialized per server. Only safe for stateless servers (x);
#   stateful servers must stay per-connection.


def shared_kind(server: str, *, home: Path) -> object:
    """The server's `shared` declaration: `"browser"`, True, or falsy (none)."""
    spec = load_mcp_config(home=home).get(server)
    return spec.get("shared") if spec else None


@dataclass
class Buckets:
    """One set of cached MCP sessions: the session per server, the transport stack
    that closes it, and the lock that serializes its (re)connect."""

    sessions: dict[str, Any] = field(default_factory=dict[str, Any])
    stacks: dict[str, AsyncExitStack] = field(default_factory=dict[str, AsyncExitStack])
    locks: dict[str, asyncio.Lock] = field(default_factory=dict[str, asyncio.Lock])

    async def close(self) -> None:
        """Close every cached session's transport and forget them all."""
        for server, stack in self.stacks.items():
            try:
                await stack.aclose()
            except Exception:
                logger.opt(exception=True).warning(
                    "closing the transport stack of MCP server {} failed; its session is dropped anyway",
                    server,
                )
        self.sessions.clear()
        self.stacks.clear()


@dataclass
class Scope:
    """What one client connection reaches: its own buckets, plus the daemon-wide
    state every connection shares. `run_daemon` builds the shared half once;
    `handle_connection` pairs it with a fresh `local` per connection.

    `"shared": true` servers use `shared` (one child for every agent connection,
    released only at daemon shutdown). Everything else, including
    `"shared": "browser"` / `"shared": "computer_use"`, which dial the per-machine
    browser-mcp / computer-mcp services directly, uses `local`: each connection
    keeps its own socket, so the service's per-connection affinity (page context
    / agent identity) still isolates agents (a single daemon-wide socket would
    both collapse every agent onto one context and let concurrent connections
    corrupt each other's request-id stream).

    `oauth_locks` serializes one in-flight authorization flow per server across
    connections (other connections wait while a flow runs, then find the stored
    tokens).
    """

    local: Buckets
    shared: Buckets
    oauth_locks: dict[str, asyncio.Lock]
    config: DaemonConfig

    def buckets_for(self, shared: object) -> Buckets:
        """Which buckets a server's sessions live in, from its `shared` declaration."""
        return self.shared if shared is True else self.local


@dataclass
class DaemonWide:
    """The state every connection of one daemon shares; `run_daemon` builds it once."""

    config: DaemonConfig
    shared: Buckets = field(default_factory=Buckets)
    oauth_locks: dict[str, asyncio.Lock] = field(default_factory=dict[str, asyncio.Lock])


class SerialSession:
    """Serialize every call on a daemon-wide shared MCP session.

    Concurrent calls from different agent connections would interleave on the
    shared stdio pipe and inside single-threaded servers (x_mcp's read-one-line-at-a-time bridge). One call at a time keeps
    semantics identical to the browser-mcp daemon's serial choice; only the
    shared session is serialized, never the per-connection ones.
    """

    def __init__(self, session: DirectSession, lock: asyncio.Lock) -> None:
        self._session = session
        self._lock = lock

    async def list_tools(self) -> ListToolsResult:
        async with self._lock:
            return await self._session.list_tools()

    async def call_tool(self, name: str, args: dict[str, Any]) -> CallToolResult:
        async with self._lock:
            result = await self._session.call_tool(name, args)
            if not isinstance(result, CallToolResult):
                raise TypeError(f"Expected CallToolResult, got {type(result).__name__}")
            return result


async def invalidate_session(
    server: str,
    scope: Scope,
) -> None:
    """Remove *server*'s dead cached session and close its resources (under
    the session lock to avoid racing a concurrent reconnect). Resolves the
    shared daemon-wide buckets for shared servers, per-connection otherwise."""
    buckets = scope.buckets_for(shared_kind(server, home=scope.config.home))
    lock = buckets.locks.get(server)
    if lock is None:
        return
    async with lock:
        buckets.sessions.pop(server, None)
        stack = buckets.stacks.pop(server, None)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                logger.opt(exception=True).warning(
                    "closing the transport stack of MCP server {} failed; "
                    "the next connect spawns a fresh one anyway",
                    server,
                )


# meiva:note
# Start MCP server `server` and return (session, AsyncExitStack); the caller owns the stack. The entry decides the transport (`plan_connect`): a `url` entry is dialed over Streamable HTTP, `"shared": "browser"` and `"shared": "computer_use"` dial the per-machine service with no child, and everything else spawns a stdio child. A server whose `requires` this host cannot meet raises before anything starts.
async def connect_server(
    server: str, config: DaemonConfig, oauth_locks: dict[str, asyncio.Lock]
) -> tuple[DirectSession, AsyncExitStack]:
    spec = load_mcp_config(home=config.home)[server]
    unmet = unmet_requirement(
        validate_requirements(spec), display_available=config.display_available
    )
    if unmet is not None:
        raise MCPError(unmet)
    plan = plan_connect(server, spec, url=server_url(spec))
    if isinstance(plan, HttpConnect):
        return await connect_http(plan, oauth_locks, config.connect_timeout_seconds, config.home)
    if isinstance(plan, StdioConnect):
        return await connect_stdio(server, plan, config.connect_timeout_seconds, home=config.home)
    if plan.service == "browser":
        return await connect_browser_direct(config.home, config.browser_cdp_port)
    return await connect_computer_direct(str(computer_mcp_socket(config.home)))


# meiva:note
# Spawn the stdio child of `plan` and initialize an MCP session over it; the child's stderr is discarded. On failure the local stack is closed so no resources (subprocess, pipes, fds) leak.
async def connect_stdio(
    server: str, plan: StdioConnect, timeout_seconds: float, *, home: Path
) -> tuple[ClientSession, AsyncExitStack]:
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    # meiva:note
    # Our own servers launch their interpreter by a relative path, which resolves against the child's cwd: installed packages from their own dir (isolated venv), built-ins from the repo root. Plugin and machine entries get None.
    cwd = server_cwd(server, home=home)
    params = StdioServerParameters(
        command=plan.command,
        args=list(plan.args),
        env=dict(plan.env) if plan.env else None,
        cwd=cwd,
    )
    local_stack = AsyncExitStack()
    try:
        devnull = open(os.devnull, "w")  # noqa: SIM115, PTH123
        try:
            read, write = await asyncio.wait_for(
                local_stack.enter_async_context(stdio_client(params, errlog=devnull)),
                timeout=timeout_seconds,
            )
        except BaseException:
            devnull.close()
            raise
        devnull.close()
        session = await local_stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=timeout_seconds)
    except BaseException:
        await local_stack.aclose()
        raise
    return session, local_stack


# meiva:note
# Connect to a remote Streamable HTTP MCP server, with no child process. The 2026-07-28 protocol revision is stateless over HTTP: every request is self-describing (protocol version and client capabilities ride in `_meta`), so one endpoint serves every agent connection with no session state on either side. Static auth (API keys) goes in `plan.headers` via the SDK's own client factory; `plan.oauth` builds an OAuth 2.1 provider instead (authorization code + PKCE, browser flow, see `meiva/mcps/_oauth.py`). An OAuth connect envelope is far more generous than `timeout_seconds`, since the user clicks through a browser. On failure the local stack is closed so no HTTP client leaks; the caller owns the returned stack. `create_mcp_http_client` is re-exported from `mcp.shared._httpx_utils`, which pyright reports as a private import.
async def connect_http(
    plan: HttpConnect, oauth_locks: dict[str, asyncio.Lock], timeout_seconds: float, home: Path
) -> tuple[ClientSession, AsyncExitStack]:
    from mcp.client.streamable_http import (
        create_mcp_http_client,  # pyright: ignore[reportPrivateImportUsage]
        streamable_http_client,
    )

    local_stack = AsyncExitStack()
    timeout = OAUTH_FLOW_TIMEOUT_S if plan.oauth else timeout_seconds
    try:
        if plan.oauth:
            from ._oauth import oauth_http_client

            http_client = await oauth_http_client(plan.url, plan.server, oauth_locks, home)
        else:
            http_client = (
                create_mcp_http_client(headers=dict(plan.headers)) if plan.headers else None
            )
        read, write = await asyncio.wait_for(
            local_stack.enter_async_context(
                streamable_http_client(plan.url, http_client=http_client)
            ),
            timeout=timeout,
        )
        session = await local_stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=timeout)
    except BaseException:
        await local_stack.aclose()
        raise
    return session, local_stack


async def _handle_list_tools(req_id: object, server: str, scope: Scope) -> dict[str, object]:
    """List the configured server's tools in the daemon wire format."""
    session = await get_session(server, scope)
    result = await session.list_tools()
    return tools_response(req_id, result.tools)


async def _handle_call_tool(
    req: dict[str, Any], req_id: object, params: dict[str, Any], server: str, scope: Scope
) -> dict[str, object]:
    """Call one MCP tool and serialize its result for the daemon wire protocol."""
    tool = params.get("tool", "")
    args: dict[str, Any] = params.get("args") or {}
    session = await get_session(server, scope)
    # meiva:note
    # The direct-dial sessions carry the calling agent's identity to their
    # respective daemons (page affinity + governance/audit).
    if isinstance(session, (BrowserLineSession, ComputerLineSession)):
        session.client_agent_id = req.get("agent_id")
    try:
        result = await session.call_tool(tool, args)
    except Exception as e:
        if not is_transport_error(e):
            raise
        await invalidate_session(server, scope)
        raise MCPCallError(
            f"MCP tool result unknown for {server}.{tool}; request may have executed: "
            f"{type(e).__name__}: {e}"
        ) from e
    if not isinstance(result, CallToolResult):
        raise TypeError(f"Expected CallToolResult, got {type(result).__name__}")
    return call_response(req_id, result)


async def _dispatch_request(
    req: dict[str, Any],
    req_id: object,
    method: object,
    params: dict[str, Any],
    server: str,
    scope: Scope,
) -> dict[str, object]:
    """Dispatch one parsed client request to its protocol method handler."""
    if method == "ping":
        return ping_response(req_id)
    if method == "list_tools":
        return await _handle_list_tools(req_id, server, scope)
    if method == "call_tool":
        return await _handle_call_tool(req, req_id, params, server, scope)
    return error_response(req_id, f"Unknown method: {method}")


# meiva:note
# Serve one request; one bad call becomes an error response, not a dropped connection. A transport failure before a tool call starts, or while listing tools, drops the dead session and tries again after the delay `retry_delay` names. A started tool call converts an uncertain transport failure to MCPCallError before reaching here, so it is never replayed.
async def dispatch_with_retry(req: dict[str, Any], scope: Scope) -> dict[str, object]:
    req_id = req.get("id")
    method = req.get("method")
    params: dict[str, Any] = req.get("params") or {}
    server: str = params.get("server", "")
    attempt = 0
    try:
        while True:
            try:
                return await _dispatch_request(req, req_id, method, params, server, scope)
            except Exception as e:
                delay = retry_delay(
                    scope.config.retry, attempt, transport_error=is_transport_error(e)
                )
                if delay is None:
                    raise
                logger.opt(exception=True).warning(
                    "MCP server {} transport error (attempt {}); dropping its session and retrying",
                    server,
                    attempt + 1,
                )
                await invalidate_session(server, scope)
                await asyncio.sleep(delay)
                attempt += 1
    except Exception as e:
        return error_response(req_id, f"{type(e).__name__}: {e}")


# meiva:note
# Handle one Unix socket client connection: one JSON line in, one response line out, until the client drops. A line that is not JSON gets a parse-error response and the loop resyncs on the next line.
async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, scope: Scope
) -> None:
    try:
        # Client may drop mid-stream (peer reset / pipe closed) — that ends the
        # connection, not an error; the finally still closes our writer.
        with suppress(ConnectionResetError, BrokenPipeError):
            while line := await reader.readline():
                try:
                    req = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as e:
                    writer.write(encode_line(error_response(None, f"JSON parse error: {e}")))
                    await writer.drain()
                    continue
                writer.write(encode_line(await dispatch_with_retry(req, scope)))
                await writer.drain()
    finally:
        writer.close()
        with suppress(OSError):  # the client is already gone: reset / broken pipe on close
            await writer.wait_closed()


async def get_session(server: str, scope: Scope) -> ManagedSession:
    """Get or create MCP server session (thread-safe).

    Shared servers (`"shared": true` in their spec) live in the daemon-wide
    buckets and are wrapped in a serializing session, so every connection
    shares one stdio child instead of spawning its own; non-shared servers
    keep the per-connection isolation contract."""
    cfg = load_mcp_config(home=scope.config.home)
    if server not in cfg:
        raise ValueError(f"Server {server!r} not configured")
    shared = cfg[server].get("shared")
    buckets = scope.buckets_for(shared)
    if server in buckets.sessions:
        cached: ManagedSession = buckets.sessions[server]
        return cached

    lock = buckets.locks.get(server)
    if lock is None:
        lock = asyncio.Lock()
        buckets.locks[server] = lock

    async with lock:
        cfg = load_mcp_config(home=scope.config.home)
        if server not in cfg:
            raise ValueError(f"Server {server!r} not configured")
        if server in buckets.sessions:
            cached_locked: ManagedSession = buckets.sessions[server]
            return cached_locked

        raw_session, stack = await connect_server(server, scope.config, scope.oauth_locks)
        session: ManagedSession = (
            SerialSession(raw_session, lock) if shared is True else raw_session
        )
        buckets.sessions[server] = session
        buckets.stacks[server] = stack
        return session


async def handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, daemon: DaemonWide
) -> None:
    """One client connection = one agent's session space.

    The shared daemon serves every agent on the machine; each connection gets
    its own MCP-server session cache, so agent A's chrome session can never be
    reached by agent B (mirrors the browser-mcp daemon's per-connection page
    affinity). When the connection closes — agent exit, restart, crash — its
    sessions are cleaned up so no MCP server subprocess outlives its owner.
    `"shared"` servers live in the daemon-wide buckets instead: they outlive
    the connection by design and are released at daemon shutdown.
    """
    scope = Scope(
        local=Buckets(),
        shared=daemon.shared,
        oauth_locks=daemon.oauth_locks,
        config=daemon.config,
    )
    try:
        await handle_client(reader, writer, scope)
    finally:
        await scope.local.close()


def _prepare_socket(socket_path: str, config: DaemonConfig) -> None:
    """Clear the way for a fresh bind: create the socket's directory (the path helpers derive
    paths and create nothing), reap stale daemons, drop a stale file."""
    Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
    reap_stale_daemons(root=str(config.project_root), home=str(config.home), me=config.pid)
    with suppress(OSError):
        Path(socket_path).unlink()


async def run_daemon(socket_path: str, config: DaemonConfig) -> None:
    """Start the shared MCP daemon, listening on one Unix socket.

    A single per-machine process (ops roster session "mcp-daemon") serves every
    agent. Sessions are isolated per client connection, so the shared process is
    not a shared state.
    """
    # Reap this unit's stale daemons + drop a stale socket file, then bind.
    # The live-socket guard already ran in main(); anything still at
    # `socket_path` here is a dead file, safe to clear.
    _prepare_socket(socket_path, config)

    daemon = DaemonWide(config=config)

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_connection(reader, writer, daemon)

    server = await asyncio.start_unix_server(_serve, path=socket_path)
    # The inode WE bound: an exit cleanup must not unlink a later occupant's
    # socket file (Task #1142).
    self_ino = Path(socket_path).stat().st_ino

    logger.info("MCP daemon listening on {}", socket_path)

    # Graceful exit
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    # finally, not just after stop_event: a cancel (parent task teardown) raises
    # out of stop_event.wait(), and without this the listening socket fd would
    # leak — the server must close on every exit path, not only the graceful one.
    try:
        await stop_event.wait()
    finally:
        logger.info("MCP daemon shutting down...")
        server.close()
        await server.wait_closed()
        # Per-connection buckets are cleaned by handle_connection as each
        # client disconnects; the daemon-wide shared buckets outlive every
        # connection and are released only here.
        await daemon.shared.close()
        unlink_own_socket(socket_path, self_ino)


# meiva:note
# The daemon's composition root: the settings and host facts it reads once, as the value every later function takes. A setting changed while the daemon runs takes effect at the next daemon start.
def daemon_config_from_process() -> DaemonConfig:
    project_root = settings.services.project_root
    return DaemonConfig(
        connect_timeout_seconds=settings.sandbox.mcp_connect_timeout_seconds,
        display_available=display_available(host=Host.system(environ=os.environ)),
        home=meiva_home(),
        project_root=project_root
        if project_root is not None
        else Path(__file__).resolve().parents[2],
        browser_cdp_port=settings.services.browser_cdp_port,
        pid=os.getpid(),
    )


# meiva:note
# Entry point: python -m meiva.mcps._daemon [socket_path]. No argument binds the shared per-machine socket (normal operation, run as the "mcp-daemon" ops service); one argument binds that path (tests, migration); two or more fail fast. It refuses to start over a LIVE socket, since unlinking and rebinding is what turns the serving daemon into a ghost when a respawn storm races the previous instance's exit (Task #1142): the healthcheck's probe answers a ping, so an alive daemon keeps the watchdog from respawning over it, and a stray launch exits cleanly instead of stealing the socket.
def main() -> None:
    config = daemon_config_from_process()
    socket_path = socket_for_args(sys.argv[1:], str(mcp_daemon_shared_socket(config.home)))
    if socket_path is None:
        logger.error(USAGE)
        sys.exit(1)
    add_sink(sys.stderr, format="{message}")
    occupied = Path(socket_path).exists() and socket_is_live(
        socket_path, timeout_seconds=config.socket_probe_timeout_seconds
    )
    refusal = refusal_to_start(socket_path, occupied=occupied)
    if refusal is not None:
        logger.error(refusal)
        sys.exit(1)
    asyncio.run(run_daemon(socket_path, config))


if __name__ == "__main__":
    main()
