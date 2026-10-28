# meiva:note
# Per-machine shared chrome-devtools-mcp service.
#
# One upstream `chrome-devtools-mcp` attached to the shared headed Chrome,
# multiplexed over a Unix socket to every agent's chrome MCP bridge
# (`services.desktop.browser.mcp_wrapper`). One shared upstream collects each tab once.
#
# Two invariants make one upstream safe for many clients:
# - Serial: a single lock around every upstream interaction so concurrent clients never interleave.
# - Per-connection page affinity: each connection/agent tracks its own current page and re-selects
#   it before forwarding a page-scoped call, cold-starting or returning the no-page error when empty.
#
# Page lifetime (2026-09-11 ruling): every page this stack creates carries a hard TTL deadline
# (`MEIVA_CHROME_PAGE_DEFAULT_TTL_SECONDS`, 24h default) and is closed by the expiry sweep when
# the deadline passes. `renew_page` moves the deadline to now + ttl (at most 24h per call); see
# `services.desktop.browser.page_lifecycle`. Every page an agent creates is also filed into that
# agent's Chrome tab group (`services.desktop.browser.tab_groups`, presentation-only).

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, types
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import GatewayAccess
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig, db_config_from_boot
from base.log import logger
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import chrome_mcp_socket, meiva_home
from base.telemetry import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.desktop.browser import page_lifecycle, session_restore
from services.desktop.browser.gateway_session import GatewaySession, stop_on_signals
from services.desktop.browser.mcp_upstream import (
    StoppingError,
    await_stop_or_timeout,
    bounded,
    bounded_stack_close,
    contained,
    create_upstream,
)
from services.desktop.browser.page_lifecycle import (
    RENEW_PAGE_TOOL,
    PageRegistry,
    expired_page_reaper,
    handle_release_agent_page,
    renew_agent_page,
    renew_page_tool_dump,
    text_of,
)
from services.desktop.browser.protocol import Request, Response
from services.desktop.browser.tab_groups.grouping import AgentTabGroups

# meiva:note
# Multi-MB tool results (screenshot / DOM snapshot) fit on one line.
_LINE_LIMIT = 64 * 1024 * 1024

# meiva:note
# Page-management tools change which page a connection owns (or are browser-global)
# and are not re-pinned to the connection's current page before forwarding.
_MANAGEMENT_TOOLS = frozenset({"new_page", "select_page", "close_page", "list_pages"})
_NO_PAGE_MARKER = "No page selected"

# meiva:note
# Active tab line `<id>: <url> [selected]` with optional `key=value` suffixes.
_SELECTED_RE = re.compile(r"^\s*(\d+):.*\[selected\](?:\s+\S+=\S+)*\s*$", re.MULTILINE)

# meiva:note
# Upstream session teardown or MCP CONNECTION_CLOSED / REQUEST_TIMEOUT ends this daemon generation.
_UPSTREAM_DOWN = (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)
_UPSTREAM_DOWN_ERROR_CODES = {CONNECTION_CLOSED, REQUEST_TIMEOUT}
_UPSTREAM_DOWN_MSG = "chrome upstream session is down; restart browser-mcp before continuing"


def _is_upstream_down(exc: BaseException) -> bool:
    """True when `exc` means this upstream session is unusable."""
    return isinstance(exc, _UPSTREAM_DOWN) or (
        isinstance(exc, MCPError) and exc.code in _UPSTREAM_DOWN_ERROR_CODES
    )


# meiva:note
# Upstream watchdog: with no client calling, a wedged upstream would sit
# undetected forever (ping answers from the daemon, list_tools serves its
# cache). Probe the upstream itself every interval; a missing reply within the
# timeout marks the session dead and ends the process (2026-08-06 #899).
_UPSTREAM_WATCHDOG_INTERVAL_S = 30.0
_UPSTREAM_WATCHDOG_TIMEOUT_S = 10.0


def selected_id(result: types.CallToolResult) -> int | None:
    """The `[selected]` page id in a page-list result, or None on format drift."""
    m = _SELECTED_RE.search(text_of(result))
    return int(m.group(1)) if m else None


# meiva:note
# An int page id from a JSON argument (including integral float), or None.
def _page_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


# meiva:note
# True for a `new_page` into an isolated browser context (invisible to extensions).
def _in_isolated_context(args: dict[str, Any]) -> bool:
    return args.get("isolatedContext") is not None


# meiva:note
# The upstream's no-page error for a page-scoped call on a connection with no page.
def no_page_result() -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{_NO_PAGE_MARKER}. Open a page first.")],
        is_error=True,
    )


# meiva:note
# Local error when `renew_page` is called without an agent id.
def _renew_requires_agent_result() -> types.CallToolResult:
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text=f"{RENEW_PAGE_TOOL} requires an agent identity")
        ],
        is_error=True,
    )


# meiva:note
# Owns the single upstream session, the serial lock, and the cached tool list.
class ChromeMcpDaemon:
    def __init__(
        self,
        upstream: ClientSession,
        pages: PageRegistry,
        tab_groups: AgentTabGroups,
    ) -> None:
        self._upstream = upstream
        self.pages = pages
        self.tab_groups = tab_groups
        self._lock = asyncio.Lock()
        self._tools: list[types.Tool] | None = None
        # meiva:note
        # Set when an upstream call hits a closed session; run() watches it to
        # terminate this unusable daemon.
        self.dead = asyncio.Event()

    # meiva:note
    # Forward to upstream, flagging `dead` on session death and releasing the page's
    # tab group first on `close_page` (best-effort).
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    async def _call(self, name: str, args: dict[str, Any]) -> types.CallToolResult:
        if name == "close_page":
            closing = _page_id(args.get("pageId"))
            if closing is not None:
                await self.tab_groups.release_page(closing)
        try:
            return await self._upstream.call_tool(name, args)
        except _UPSTREAM_DOWN as e:
            self.dead.set()
            raise RuntimeError(_UPSTREAM_DOWN_MSG) from e
        except (MCPError, TimeoutError) as e:
            if _is_upstream_down(e):
                self.dead.set()
                raise RuntimeError(_UPSTREAM_DOWN_MSG) from e
            raise

    async def list_tools(self) -> list[dict[str, Any]]:
        async with self._lock:
            if self._tools is None:
                try:
                    self._tools = (await self._upstream.list_tools()).tools
                except _UPSTREAM_DOWN as e:
                    self.dead.set()
                    raise RuntimeError(_UPSTREAM_DOWN_MSG) from e
                except (MCPError, TimeoutError) as e:
                    if _is_upstream_down(e):
                        self.dead.set()
                        raise RuntimeError(_UPSTREAM_DOWN_MSG) from e
                    raise
        # meiva:note
        # Daemon-owned tool appended after the upstream's verbatim passthrough:
        # `chrome-devtools-mcp` has no TTL concept, so `renew_page` is
        # implemented entirely on this side (see `page_lifecycle`).
        return [t.model_dump(mode="json", by_alias=True) for t in self._tools] + [
            renew_page_tool_dump()
        ]

    # meiva:note
    # Forward one call under the serial lock, pinned to `current_page`.
    async def call_tool(
        self, name: str, args: dict[str, Any], current_page: int | None
    ) -> tuple[types.CallToolResult, int | None]:
        if name == RENEW_PAGE_TOOL:
            return _renew_requires_agent_result(), current_page
        async with self._lock:
            return await self._affinity_call(name, args, current_page)

    # meiva:note
    # Forward one call pinned to the agent's page (per-agent affinity).
    async def call_tool_for_agent(
        self, name: str, args: dict[str, Any], agent_id: int
    ) -> types.CallToolResult:
        if name == RENEW_PAGE_TOOL:
            return await renew_agent_page(self, agent_id, args)
        async with self._lock:
            page = self.pages.get_agent_page(agent_id)
            result, updated = await self._affinity_call(name, args, page, agent_id)
            self.pages.set_agent_page(agent_id, updated)
            return result

    # meiva:note
    # Re-pin to this connection's page, dropping affinity if the page vanished.
    async def _repin(self, current_page: int | None) -> int | None:
        if current_page is None:
            return None
        repin = await self._call("select_page", {"pageId": current_page})
        if repin.is_error:
            logger.warning(
                f"[browser-mcp] re-pin to page {current_page} failed "
                f"({text_of(repin)!r}); dropping affinity"
            )
            return None
        return current_page

    # meiva:note
    # Forward a `new_page`, register its TTL deadline, and group it for the agent.
    async def _new_page(self, args: dict[str, Any], agent_id: int | None) -> types.CallToolResult:
        group = agent_id is not None
        if group and _in_isolated_context(args):
            group = False
            logger.info(
                "[browser-mcp] agent {}'s page in isolated context {!r} is not tab-grouped "
                "(extensions cannot see isolated-context tabs)",
                agent_id,
                args["isolatedContext"],
            )
        before = await self.tab_groups.snapshot() if group else None
        result = await self._call("new_page", args)
        if not result.is_error:
            created = selected_id(result)
            self.pages.register_created_page(created, agent_id=agent_id)
            if group and agent_id is not None:
                await self.tab_groups.group_new_page(agent_id, before, created)
        return result

    async def _bootstrap_page(
        self, name: str, args: dict[str, Any], *, agent_id: int | None
    ) -> tuple[types.CallToolResult, int | None]:
        """No tab of our own to act on. navigate_page can bootstrap one; anything else fails
        fast instead of touching another client's tab."""
        if name == "navigate_page" and isinstance(args.get("url"), str):
            result = await self._new_page(args, agent_id)
            return result, (selected_id(result) or None)
        return no_page_result(), None

    async def _affinity_call(
        self,
        name: str,
        args: dict[str, Any],
        current_page: int | None,
        agent_id: int | None = None,
    ) -> tuple[types.CallToolResult, int | None]:
        page_scoped = name not in _MANAGEMENT_TOOLS

        if page_scoped:
            current_page = await self._repin(current_page)
            if current_page is None:
                return await self._bootstrap_page(name, args, agent_id=agent_id)

        # meiva:note
        # A page created here gets its TTL slot (see `page_lifecycle`) and tab
        # group in `_new_page`; a listing that drifted off the parseable shape
        # registers nothing.
        if name == "new_page":
            result = await self._new_page(args, agent_id)
        else:
            result = await self._call(name, args)
        if name == "close_page" and not result.is_error:
            # meiva:note
            # A clean close drops the TTL slot with the page; the shared
            # `_page_id` normalization accepts an integral-float pageId too
            # (bool is rejected so a JSON `true` can never alias page id 1).
            self.pages.drop_page_ttl(_page_id(args.get("pageId")))
        return result, self._next_page(name, args, result, current_page)

    @staticmethod
    def _next_page(
        name: str, args: dict[str, Any], result: types.CallToolResult, current_page: int | None
    ) -> int | None:
        """The connection's current page after this call resolved."""
        if result.is_error:
            return current_page
        if name == "new_page":
            return selected_id(result) or current_page  # the freshly opened tab
        if name == "select_page":
            pid = _page_id(args.get("pageId"))
            return pid if pid is not None else current_page
        if name == "close_page" and args.get("pageId") == current_page:
            return None
        # meiva:note
        # navigate_page (forwarded) and every other page-scoped op stay re-pinned.
        return current_page


_ConnPage = int | None  # the connection's current page


async def _call_tool_response(
    daemon: ChromeMcpDaemon, req: Request, req_id: int | None, conn_page: _ConnPage
) -> tuple[Response, _ConnPage]:
    """Serve a `call_tool` request; the response and the connection's page afterwards."""
    tool = req.get("tool")
    if not tool:
        # meiva:note
        # Missing/empty tool name: reject at the protocol edge
        # instead of forwarding a nameless call the upstream
        # would reject with a less legible error.
        return {
            "id": req_id,
            "ok": False,
            "error": "call_tool requires a non-empty 'tool' name",
        }, conn_page
    agent_id = req.get("agent_id")
    # meiva:note
    # bool is an int subclass — reject it so a JSON
    # `true` can never alias another agent's slot.
    if isinstance(agent_id, int) and not isinstance(agent_id, bool):
        result = await daemon.call_tool_for_agent(tool, req.get("args") or {}, agent_id)
    else:
        result, conn_page = await page_lifecycle.forward_legacy_call(
            daemon, tool, req.get("args") or {}, conn_page
        )
    return {"id": req_id, "ok": True, "result": result.model_dump(mode="json")}, conn_page


async def _respond(
    daemon: ChromeMcpDaemon,
    req: Request,
    req_id: int | None,
    method: str | None,
    conn_page: _ConnPage,
) -> tuple[Response, _ConnPage]:
    """Serve one request on a live daemon; one bad call must not drop the whole connection."""
    try:
        if method == "list_tools":
            return {"id": req_id, "ok": True, "result": await daemon.list_tools()}, conn_page
        if method == "call_tool":
            return await _call_tool_response(daemon, req, req_id, conn_page)
        if method == "release_agent_page":
            return await handle_release_agent_page(daemon, req, req_id), conn_page
        return {"id": req_id, "ok": False, "error": f"Unknown method: {method}"}, conn_page
    except Exception as e:
        return {"id": req_id, "ok": False, "error": f"{type(e).__name__}: {e}"}, conn_page


async def _serve_line(
    line: bytes,
    writer: asyncio.StreamWriter,
    daemon_ref: list[ChromeMcpDaemon | None],
    conn_page: _ConnPage,
) -> _ConnPage:
    """Parse, serve and answer one request line; the connection's page afterwards."""
    try:
        req: Request = json.loads(line)
    except json.JSONDecodeError as e:
        _write(writer, {"id": None, "ok": False, "error": f"JSON parse error: {e}"})
        await writer.drain()
        return conn_page

    raw_id = req.get("id")
    req_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
    raw_method = req.get("method")
    method = raw_method if isinstance(raw_method, str) else None

    # meiva:note
    # ping is lock-free and must succeed while the upstream connects:
    # the healthcheck probes the daemon process itself (not the
    # upstream), so an initial connect window should never trigger a
    # false-positive death and unnecessary respawn.
    if method == "ping":
        _write(writer, {"id": req_id, "ok": True, "result": None})
        await writer.drain()
        return conn_page

    daemon = daemon_ref[0]
    if daemon is None or daemon.dead.is_set():
        # meiva:note
        # The initial upstream connection is not ready.
        _write(writer, {"id": req_id, "ok": False, "error": _UPSTREAM_DOWN_MSG})
        await writer.drain()
        return conn_page

    resp, conn_page = await _respond(daemon, req, req_id, method, conn_page)
    _write(writer, resp)
    await writer.drain()
    return conn_page


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    daemon_ref: list[ChromeMcpDaemon | None],
) -> None:
    """One agent bridge connection. Requests carrying an `agent_id` resolve
    their page from the per-agent registry (a fresh connection — exec
    subprocess child — inherits the agent's page); the rest track their own
    page (see ``forward_legacy_call``). `daemon_ref` is a
    mutable cell so the handler always sees the current daemon across
    connects; when None (starting), it returns a pre-execution error."""
    conn_page: _ConnPage = None
    try:
        with suppress(ConnectionResetError, BrokenPipeError):
            while line := await reader.readline():
                conn_page = await _serve_line(line, writer, daemon_ref, conn_page)
    finally:
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


def _write(writer: asyncio.StreamWriter, obj: Response) -> None:
    writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())


async def _await_death_or_stop(daemon: ChromeMcpDaemon, stop: asyncio.Event) -> None:
    """Wait for daemon.dead or stop signal, cleaning up tasks on exit."""
    async with asyncio.TaskGroup() as waiters:
        dead_task = waiters.create_task(daemon.dead.wait())
        stop_task = waiters.create_task(stop.wait())
        try:
            await asyncio.wait({stop_task, dead_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            dead_task.cancel()
            stop_task.cancel()


async def _upstream_watchdog(daemon: ChromeMcpDaemon, stop: asyncio.Event) -> None:
    """Periodically ping the upstream session; a wedged one (no reply within the
    timeout) sets `dead` so run() exits even with no client calling."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                daemon._upstream.send_ping(), timeout=_UPSTREAM_WATCHDOG_TIMEOUT_S
            )
        except (TimeoutError, MCPError) as e:
            if stop.is_set():
                return
            logger.error(
                f"[browser-mcp] upstream watchdog: {type(e).__name__} — terminating the daemon"
            )
            daemon.dead.set()
            return
        except Exception as e:
            if stop.is_set():
                return
            logger.warning(f"[browser-mcp] upstream watchdog ping failed: {e!r}; retrying")
        await await_stop_or_timeout(stop, _UPSTREAM_WATCHDOG_INTERVAL_S)


async def socket_in_use(path: Path) -> bool:
    """True when a live process is already listening on ``path``.

    A successful connect proves an occupant; ``FileNotFoundError`` /
    ``ConnectionRefusedError`` mean a stale socket (nobody listening) and are
    safe to unlink. Any other error is treated as occupied — fail closed
    rather than risk stealing a live instance's socket.
    """
    try:
        reader, writer = await asyncio.open_unix_connection(path=path)
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    except OSError:
        return True
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
    del reader  # nothing to close on a StreamReader; the writer close suffices
    return True


async def _open_daemon_socket(
    home: Path, browser_cdp_port: int, daemon_ref: list[ChromeMcpDaemon | None]
) -> tuple[Path, asyncio.Server]:
    browser_url = f"http://127.0.0.1:{browser_cdp_port}"
    sock = chrome_mcp_socket(home, browser_cdp_port)
    sock.parent.mkdir(parents=True, exist_ok=True)
    # meiva:note
    # Single-instance guard: probe before unlinking so a live daemon's socket is never stolen.
    if await socket_in_use(sock):
        logger.error(
            "[browser-mcp] socket {} is already served by a live daemon — "
            "refusing to start a second instance",
            sock,
        )
        raise SystemExit(1)
    with suppress(OSError):
        sock.unlink()

    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(r, w, daemon_ref), path=str(sock), limit=_LINE_LIMIT
    )
    logger.info(f"[browser-mcp] listening on {sock} (upstream {browser_url})")
    return sock, server


async def _shutdown_daemon(
    *,
    session_task: asyncio.Task[None],
    gateway: GatewaySession,
    tab_groups: AgentTabGroups,
    current_stack: AsyncExitStack | None,
    server: asyncio.Server,
    sock: Path,
) -> None:
    logger.info("[browser-mcp] shutting down")
    session_task.cancel()
    gateway.cancel_one_shots()
    tab_groups.cancel_pending()
    # meiva:note
    # Two bounded steps, so the daemon may keep closing for up to
    # `shutdown_budget.SHUTDOWN_CEILING_S`; meiva-root's window for this unit
    # is derived from that total.
    await bounded_stack_close(current_stack, "upstream stack close during shutdown")
    server.close()
    await bounded(server.wait_closed(), "server close")
    with suppress(OSError):
        sock.unlink()


# meiva:note
# Run the browser-mcp socket server and lifecycle tasks against an already-opened
# upstream session and exit stack. Shares `_open_daemon_socket`, `_serve_generation`,
# and `_shutdown_daemon` with `run()`.
async def serve_with_upstream(
    *,
    home: Path,
    browser_cdp_port: int,
    stop: asyncio.Event,
    upstream: ClientSession,
    current_stack: AsyncExitStack | None,
    clock: Clock,
    default_ttl_seconds: float = page_lifecycle.DEFAULT_PAGE_TTL_SECONDS,
) -> None:
    daemon_ref: list[ChromeMcpDaemon | None] = [None]
    sock, server = await _open_daemon_socket(home, browser_cdp_port, daemon_ref)
    pages = PageRegistry(default_ttl_seconds=default_ttl_seconds, clock=clock)
    async with asyncio.TaskGroup() as tasks:
        gateway = GatewaySession(
            tasks,
            browser_cdp_port,
            access=GatewayAccess(
                gateway_url="",
                cluster_secret="",
                remote_plane=False,
                machine_token=None,
                launcher_profile=None,
                gateway_home=False,
            ),
            clock=clock,
        )
        tab_groups = AgentTabGroups(browser_cdp_port, tasks, timers=clock)
        session_task = tasks.create_task(
            contained(gateway.refresh_loop(stop), "gateway session refresh")
        )
        try:
            daemon = ChromeMcpDaemon(upstream, pages, tab_groups)
            daemon_ref[0] = daemon
            await _serve_generation(daemon, stop, gateway)
            if daemon.dead.is_set():
                raise RuntimeError("browser-mcp upstream died; explicit service restart required")
        finally:
            await _shutdown_daemon(
                session_task=session_task,
                gateway=gateway,
                tab_groups=tab_groups,
                current_stack=current_stack,
                server=server,
                sock=sock,
            )


# meiva:note
# Start the browser-mcp daemon of `home`.
async def run(
    *,
    home: Path,
    browser_cdp_port: int,
    connect_timeout_reader: Callable[[], float],
    clock: Clock,
    gateway_access: GatewayAccess,
    default_ttl_seconds: float = page_lifecycle.DEFAULT_PAGE_TTL_SECONDS,
) -> None:
    browser_url = f"http://127.0.0.1:{browser_cdp_port}"
    daemon_ref: list[ChromeMcpDaemon | None] = [None]
    sock, server = await _open_daemon_socket(home, browser_cdp_port, daemon_ref)
    pages = PageRegistry(default_ttl_seconds=default_ttl_seconds, clock=clock)
    stop = stop_on_signals()
    async with asyncio.TaskGroup() as tasks:
        gateway = GatewaySession(tasks, browser_cdp_port, access=gateway_access, clock=clock)
        tab_groups = AgentTabGroups(browser_cdp_port, tasks, timers=clock)
        session_task = tasks.create_task(
            contained(gateway.refresh_loop(stop), "gateway session refresh")
        )
        current_stack: AsyncExitStack | None = None
        try:
            try:
                session, current_stack = await create_upstream(
                    browser_url, stop, connect_timeout_reader=connect_timeout_reader
                )
            except StoppingError:
                return
            # meiva:note
            # Restore saved pins and tab groups in the background once Chrome answers.
            # meiva:link services/desktop/browser/docs/browser-session-restore.meiva.okf.md
            tasks.create_task(
                contained(
                    session_restore.reapply(
                        home, session_restore.CdpChrome(browser_cdp_port, timers=clock)
                    ),
                    "browser session restore",
                )
            )
            daemon = ChromeMcpDaemon(session, pages, tab_groups)
            daemon_ref[0] = daemon
            await _serve_generation(daemon, stop, gateway)
            if daemon.dead.is_set():
                raise RuntimeError("browser-mcp upstream died; explicit service restart required")
        finally:
            await _shutdown_daemon(
                session_task=session_task,
                gateway=gateway,
                tab_groups=tab_groups,
                current_stack=current_stack,
                server=server,
                sock=sock,
            )


async def _serve_generation(
    daemon: ChromeMcpDaemon, stop: asyncio.Event, gateway: GatewaySession
) -> None:
    """Serve one upstream connection until it dies or the stop event fires.

    The upstream watchdog and the expired-page reaper run per connection, so they live
    in a group that closes with it.
    """
    async with asyncio.TaskGroup() as helpers:
        watchdog = helpers.create_task(
            contained(_upstream_watchdog(daemon, stop), "upstream watchdog")
        )
        reaper = helpers.create_task(
            contained(expired_page_reaper(daemon, stop), "expired-page reaper")
        )
        # meiva:note
        # Repair the one-group-per-agent-per-window invariant from Chrome's own
        # state on every connection: a duplicate that outlived a Chrome restart, an
        # extension update or a race is merged here, with a WARNING, before any page
        # is created. Idempotent, so a healthy browser costs two queries.
        # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
        helpers.create_task(contained(daemon.tab_groups.converge_all(), "tab group convergence"))
        # meiva:note
        # Chrome is confirmed up here — inject the gateway session cookie
        # right away (the periodic loop covers expiry; this covers the
        # fresh-profile cold start and the post-restart gap without
        # waiting for the next tick).
        gateway.spawn_inject()
        try:
            await _await_death_or_stop(daemon, stop)
        finally:
            watchdog.cancel()
            reaper.cancel()


# meiva:note
# Process-entry wiring assembled from a booted ConfigBoot and captured LoadedCommit.
@dataclass(frozen=True)
class BrowserMcpEntryWiring:
    process_name: str
    config: ConfigBoot
    image: LoadedCommit
    process_home: Path
    machine: ConfiguredMachineIdentity
    gate: ProcessDbGate
    browser_cdp_port: int
    default_ttl_seconds: float
    clock: Clock
    gateway_access: GatewayAccess

    def db_config(self) -> DbConfig:
        return db_config_from_boot(self.config)

    def direct_url(self) -> ClusterDirectUrl:
        return ClusterDirectUrl(self.machine.host, home=self.process_home)

    def database(self) -> Database:
        return Database(
            self.db_config(),
            gate=self.gate,
            direct_url=self.direct_url(),
        )

    def connect_timeout_seconds(self) -> float:
        return self.config.view.sandbox.mcp_connect_timeout_seconds


# meiva:note
# Assemble the process-entry collaborators from a booted ConfigBoot and captured LoadedCommit
# so `main()` wires telemetry, database gate, and daemon runtime parameters from one owner.
def build_entry_wiring(
    *, config: ConfigBoot, image: LoadedCommit, home: Path
) -> BrowserMcpEntryWiring:
    machine = ConfiguredMachineIdentity(config)
    process_home = home.resolve()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="browser-mcp")
    return BrowserMcpEntryWiring(
        process_name="browser-mcp",
        config=config,
        image=image,
        process_home=process_home,
        machine=machine,
        gate=gate,
        browser_cdp_port=config.view.services.browser_cdp_port,
        default_ttl_seconds=float(config.view.daemon.chrome_page_default_ttl_seconds),
        clock=Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        gateway_access=gateway_access_from_boot(config),
    )


def main() -> None:
    # meiva:note
    # Same boot seam every long-running service shares (cf. computer-mcp): a
    # per-daemon log file plus the event pipeline, so this daemon's expiry /
    # renewal events land attributed to `browser-mcp` and an uncaught
    # traceback is postmortem-able. Idempotent.
    image = LoadedCommit.capture()
    config = ConfigBoot()
    config.boot()
    wiring = build_entry_wiring(config=config, image=image, home=meiva_home())
    pipeline = build_pipeline(
        database=wiring.database, machine=wiring.machine, home=wiring.process_home
    )
    init_gateway_process(
        name=wiring.process_name,
        producer=lambda: pipeline,
        machine_reader=wiring.machine.name,
        image=wiring.image,
        home=wiring.process_home,
    )
    asyncio.run(
        run(
            home=wiring.process_home,
            browser_cdp_port=wiring.browser_cdp_port,
            connect_timeout_reader=lambda: config.view.sandbox.mcp_connect_timeout_seconds,
            clock=wiring.clock,
            gateway_access=wiring.gateway_access,
            default_ttl_seconds=wiring.default_ttl_seconds,
        )
    )


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
