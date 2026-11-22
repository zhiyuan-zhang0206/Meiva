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
# (`MEIVA_CHROME_PAGE_DEFAULT_TTL_SECONDS`, 24h default), kept in a per-home ledger file that
# survives a daemon restart, and is closed by the expiry sweep when the deadline passes. `renew_page` moves the deadline to now + ttl (at most 24h per call); see
# `services.desktop.browser.page_lifecycle`. Every page an agent creates is also filed into that
# agent's Chrome tab group (`services.desktop.browser.tab_groups`, presentation-only).

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from pathlib import Path

from mcp import ClientSession, types

from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import GatewayAccess
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.log import logger
from base.native_process.loaded_commit import LoadedCommit
from base.paths import chrome_mcp_socket, meiva_home
from base.telemetry import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.desktop.browser import page_lifecycle, session_restore
from services.desktop.browser.gateway_session import GatewaySession, stop_on_signals
from services.desktop.browser.mcp_upstream import (
    DeathReason,
    StoppingError,
    UpstreamGuard,
    await_death_or_stop,
    bounded,
    bounded_stack_close,
    contained,
    create_upstream,
    report_daemon_exit,
    upstream_watchdog,
)
from services.desktop.browser.page_lifecycle import (
    RENEW_PAGE_TOOL,
    expired_page_reaper,
    renew_agent_page,
    renew_page_tool_dump,
    text_of,
)
from services.desktop.browser.routing.cdp_screenshot import capture_owned_viewport
from services.desktop.browser.routing.remote_pages import (
    PAGE_LIST_TOOLS,
    PAGE_SCOPED_TOOLS,
    authorize_page,
    bound_page_id_required,
    public_page_schema,
    reconnected,
    scope_page_list,
)
from services.desktop.browser.routing.staging import BrowserStaging
from services.desktop.browser.routing.wire_server import serve_line
from services.desktop.browser.tab_groups.grouping import (
    AgentTabGroups,
    ExtensionWorker,
    staged_extension_dir,
)
from services.desktop.browser.tab_groups.page_registry import PageRegistry
from services.desktop.browser.tab_groups.page_store import (
    FileRecordStore,
    FileSelectionStore,
    page_ledger_path,
    selection_path,
)

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
def _in_isolated_context(args: dict[str, object]) -> bool:
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
        guard: UpstreamGuard,
    ) -> None:
        self._upstream = upstream
        self.pages = pages
        self.tab_groups = tab_groups
        self.guard = guard
        self._lock = asyncio.Lock()
        self._tools: dict[str, types.Tool] | None = None
        # meiva:note
        # Set by the guard when the upstream session is judged unusable; run() watches
        # it to terminate this unusable daemon.
        self.dead = guard.dead
        self.staging: BrowserStaging | None = None
        self.capture_port: int | None = None

    # meiva:note
    # Forward to upstream under the guard's verdict (`UpstreamGuard.request`: a dead
    # session flags `dead`, a read timeout fails this call alone while the upstream
    # still answers) and release the page's tab group first on `close_page`
    # (best-effort).
    # meiva:link services/desktop/browser/docs/tab-groups.md
    async def _call(self, name: str, args: dict[str, object]) -> types.CallToolResult:
        if name == "close_page":
            closing = _page_id(args.get("pageId"))
            if closing is not None:
                await self.tab_groups.release_page(closing)
        return await self.guard.request(
            f"browser tool {name!r}", lambda: self._upstream.call_tool(name, args)
        )

    async def list_tools(self) -> list[dict[str, object]]:
        async with self._lock:
            tools = await self._tool_catalog()
        # meiva:note
        # Daemon-owned tool appended after adapting the upstream tool schema:
        # `chrome-devtools-mcp` has no TTL concept, so `renew_page` is
        # implemented entirely on this side (see `page_lifecycle`).
        return [
            public_page_schema(tool).model_dump(mode="json", by_alias=True)
            for tool in tools.values()
        ] + [renew_page_tool_dump()]

    async def _tool_catalog(self) -> dict[str, types.Tool]:
        if self._tools is None:
            listed = await self.guard.request("tool listing", self._upstream.list_tools)
            self._tools = {tool.name: tool for tool in listed.tools}
        return self._tools

    # meiva:note
    # Forward one call under the serial lock, pinned to `current_page`.
    async def call_tool(
        self, name: str, args: dict[str, object], current_page: int | None
    ) -> tuple[types.CallToolResult, int | None]:
        if name == RENEW_PAGE_TOOL:
            return _renew_requires_agent_result(), current_page
        async with self._lock:
            return await self._affinity_call(name, args, current_page)

    # meiva:note
    # Forward one call pinned to the agent's page (per-agent affinity).
    async def call_tool_for_agent(
        self, name: str, args: dict[str, object], agent_id: int, *, remote: bool = False
    ) -> types.CallToolResult:
        if name == RENEW_PAGE_TOOL:
            return await renew_agent_page(self, agent_id, args)
        async with self._lock:
            page = await page_lifecycle.restored_page(self, agent_id)
            if remote:
                return await self._remote_affinity_call(name, args, page, agent_id)
            return await self._remember_call(name, args, page, agent_id)

    async def _remember_call(
        self, name: str, args: dict[str, object], page: int | None, agent_id: int
    ) -> types.CallToolResult:
        result, updated = await self._affinity_call(name, args, page, agent_id)
        self.pages.set_agent_page(agent_id, updated)
        if updated != page:
            await page_lifecycle.remember_selection(self, agent_id, updated, result)
        return result

    async def _remote_affinity_call(
        self, name: str, args: dict[str, object], page: int | None, agent_id: int
    ) -> types.CallToolResult:
        def owns(page_id: int) -> bool:
            record = self.pages.record_for_page(page_id)
            return record is not None and record.owner == agent_id

        authorize_page(name, args, owns)
        if page is not None and not owns(page):
            page = None
        if isinstance(context := args.get("isolatedContext"), str):
            args = {**args, "isolatedContext": f"meiva-agent-{agent_id}:{context}"}
        if (
            name in PAGE_SCOPED_TOOLS
            and (selected := _page_id(args.get("pageId")) or page) is not None
        ):
            result = await self._call_owned_page(name, args, selected)
        else:
            result = await self._remember_call(name, args, page, agent_id)
        if reconnected(result):
            self.pages.page_targets.clear()
            self.pages.affinity.clear()
            raise ValueError("Remote Chrome reconnected. Open an owned page before calling tools.")
        if name in PAGE_LIST_TOOLS:
            return scope_page_list(result, owns, self.pages.get_agent_page(agent_id))
        return result

    async def _call_owned_page(
        self,
        name: str,
        args: dict[str, object],
        page: int,
    ) -> types.CallToolResult:
        if name == "take_screenshot":
            target = self.pages.page_targets.get(page)
            if target is None or self.capture_port is None:
                raise ValueError("Open an owned Chrome page before taking a screenshot")
            return await capture_owned_viewport(self.capture_port, target, args)
        return await self._call(name, {**args, "pageId": page})

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
    # Forward a `new_page` and record its TTL under the target of the page it made
    # (found between a target snapshot before and after). A page for an agent is
    # also filed into the agent's tab group, except in an isolated browser context
    # (extensions cannot see those tabs): that page is recorded, never grouped.
    async def _new_page(
        self, args: dict[str, object], agent_id: int | None
    ) -> types.CallToolResult:
        group = agent_id is not None and not _in_isolated_context(args)
        if agent_id is not None and not group:
            logger.info(
                "[browser-mcp] agent {}'s page in isolated context {!r} is not tab-grouped "
                "(extensions cannot see isolated-context tabs)",
                agent_id,
                args["isolatedContext"],
            )
        before = await self.tab_groups.snapshot() if agent_id is not None else None
        result = await self._call("new_page", args)
        if not result.is_error and agent_id is not None:
            created = selected_id(result)
            if group:
                target_id = await self.tab_groups.group_new_page(
                    agent_id, before, created, on_grouped=self.pages.mark_grouped
                )
            else:
                target_id = await self.tab_groups.new_target(agent_id, before)
            self.pages.register_created_page(target_id, agent_id, created)
        return result

    async def _bootstrap_page(
        self, name: str, args: dict[str, object], *, agent_id: int | None
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
        args: dict[str, object],
        current_page: int | None,
        agent_id: int | None = None,
    ) -> tuple[types.CallToolResult, int | None]:
        page_scoped = name not in _MANAGEMENT_TOOLS

        if page_scoped:
            current_page = await self._repin(current_page)
            if current_page is None:
                return await self._bootstrap_page(name, args, agent_id=agent_id)
            tool = (await self._tool_catalog()).get(name)
            if "pageId" not in args and tool is not None and bound_page_id_required(tool):
                args = {**args, "pageId": current_page}

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
        name: str, args: dict[str, object], result: types.CallToolResult, current_page: int | None
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


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    daemon_ref: list[ChromeMcpDaemon | None],
    clients: set[asyncio.Task[None]] | None = None,
) -> None:
    """One agent bridge connection. Requests carrying an `agent_id` resolve
    their page from the per-agent registry (a fresh connection — exec
    subprocess child — inherits the agent's page); the rest track their own
    page (see ``forward_legacy_call``). `daemon_ref` is a
    mutable cell so the handler always sees the current daemon across
    connects; when None (starting), it returns a pre-execution error."""
    task = asyncio.current_task()
    if clients is not None and task is not None:
        clients.add(task)
    conn_page: _ConnPage = None
    try:
        with suppress(ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            while line := await reader.readline():
                conn_page = await serve_line(line, writer, daemon_ref[0], conn_page)
    finally:
        if clients is not None and task is not None:
            clients.discard(task)
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()


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
    home: Path,
    browser_cdp_port: int,
    daemon_ref: list[ChromeMcpDaemon | None],
    clients: set[asyncio.Task[None]],
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
        lambda r, w: handle_client(r, w, daemon_ref, clients), path=str(sock), limit=_LINE_LIMIT
    )
    logger.info(f"[browser-mcp] listening on {sock} (upstream {browser_url})")
    return sock, server


async def _shutdown_daemon(
    *,
    session_task: asyncio.Task[None],
    reapply_task: asyncio.Task[None] | None,
    gateway: GatewaySession,
    tab_groups: AgentTabGroups,
    current_stack: AsyncExitStack | None,
    server: asyncio.Server,
    clients: set[asyncio.Task[None]],
    sock: Path,
) -> None:
    logger.info("[browser-mcp] shutting down")
    session_task.cancel()
    if reapply_task is not None:
        reapply_task.cancel()
    gateway.cancel_one_shots()
    tab_groups.cancel_pending()
    # meiva:note
    # Two bounded steps, so the daemon may keep closing for up to
    # `shutdown_budget.SHUTDOWN_CEILING_S`; meiva-root's window for this unit
    # is derived from that total.
    await bounded_stack_close(current_stack, "upstream stack close during shutdown")
    server.close()
    for client in tuple(clients):
        client.cancel()
    await bounded(server.wait_closed(), "server close")
    with suppress(OSError):
        sock.unlink()


async def _serve_daemon_loop(
    *,
    home: Path,
    browser_cdp_port: int,
    stop: asyncio.Event,
    clock: Clock,
    gateway_access: GatewayAccess,
    default_ttl_seconds: float,
    open_upstream: Callable[[], Awaitable[tuple[ClientSession, AsyncExitStack | None]]],
    reapply_session: bool,
) -> None:
    daemon_ref: list[ChromeMcpDaemon | None] = [None]
    clients: set[asyncio.Task[None]] = set()
    sock, server = await _open_daemon_socket(home, browser_cdp_port, daemon_ref, clients)
    pages = PageRegistry(
        default_ttl_seconds=default_ttl_seconds,
        clock=clock,
        store=FileRecordStore(page_ledger_path(home)),
        selection_store=FileSelectionStore(selection_path(home)),
    )
    async with asyncio.TaskGroup() as tasks:
        gateway = GatewaySession(tasks, browser_cdp_port, access=gateway_access, clock=clock)
        extension_worker = ExtensionWorker(
            browser_cdp_port,
            timers=clock,
            extension_dir=staged_extension_dir(home),
        )
        tab_groups = AgentTabGroups(browser_cdp_port, tasks, worker=extension_worker)
        session_task = tasks.create_task(
            contained(gateway.refresh_loop(stop), "gateway session refresh")
        )
        reapply_task: asyncio.Task[None] | None = None
        current_stack: AsyncExitStack | None = None
        try:
            try:
                session, current_stack = await open_upstream()
            except StoppingError:
                report_daemon_exit(DeathReason.STOPPED)
                return
            if reapply_session:
                # meiva:note
                # Restore saved pins and tab groups in the background once Chrome answers.
                # meiva:link services/desktop/browser/docs/browser-session-restore.md
                reapply_task = tasks.create_task(
                    contained(
                        session_restore.reapply(
                            home,
                            session_restore.CdpChrome(browser_cdp_port, worker=extension_worker),
                        ),
                        "browser session restore",
                    )
                )
            guard = UpstreamGuard(session, clock.timers)
            daemon = ChromeMcpDaemon(session, pages, tab_groups, guard)
            daemon.capture_port = browser_cdp_port
            daemon.staging = BrowserStaging(
                sock.with_suffix(".staging"),
                lambda tool, args, agent_id: daemon.call_tool_for_agent(
                    tool, args, agent_id, remote=True
                ),
            )
            daemon_ref[0] = daemon
            await _serve_generation(daemon, stop, gateway)
            report_daemon_exit(guard.reason or DeathReason.STOPPED)
            if daemon.dead.is_set():
                raise RuntimeError("browser-mcp upstream died; explicit service restart required")
        finally:
            await _shutdown_daemon(
                session_task=session_task,
                reapply_task=reapply_task,
                gateway=gateway,
                tab_groups=tab_groups,
                current_stack=current_stack,
                server=server,
                clients=clients,
                sock=sock,
            )


# meiva:note
# Run the browser-mcp socket server and lifecycle tasks against an already-opened
# upstream session and exit stack.
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
    async def existing_upstream() -> tuple[ClientSession, AsyncExitStack | None]:
        return upstream, current_stack

    await _serve_daemon_loop(
        home=home,
        browser_cdp_port=browser_cdp_port,
        stop=stop,
        clock=clock,
        gateway_access=GatewayAccess(
            gateway_url="",
            cluster_secret="",
            remote_plane=False,
            machine_token=None,
            launcher_profile=None,
            gateway_home=False,
        ),
        default_ttl_seconds=default_ttl_seconds,
        open_upstream=existing_upstream,
        reapply_session=False,
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
    stop = stop_on_signals()
    await _serve_daemon_loop(
        home=home,
        browser_cdp_port=browser_cdp_port,
        stop=stop,
        clock=clock,
        gateway_access=gateway_access,
        default_ttl_seconds=default_ttl_seconds,
        open_upstream=lambda: create_upstream(
            browser_url, stop, connect_timeout_reader=connect_timeout_reader, explicit_pages=True
        ),
        reapply_session=True,
    )


# meiva:note
# Serve one upstream connection until it dies or the stop event fires. The
# upstream watchdog and the expired-page reaper run per connection, so they live
# in a group that closes with it.
async def _serve_generation(
    daemon: ChromeMcpDaemon, stop: asyncio.Event, gateway: GatewaySession
) -> None:
    async with asyncio.TaskGroup() as helpers:
        watchdog = helpers.create_task(
            contained(
                upstream_watchdog(daemon.guard, stop),
                "upstream watchdog",
            )
        )
        reaper = helpers.create_task(
            contained(expired_page_reaper(daemon, stop), "expired-page reaper")
        )
        # meiva:note
        # Repair the one-group-per-agent invariant from Chrome's own state on
        # every connection: a duplicate that outlived a Chrome restart, an
        # extension update or a race is merged here, with a WARNING, before any
        # page is created. Idempotent, so a healthy browser costs one query.
        # meiva:link services/desktop/browser/docs/tab-groups.md
        converge = helpers.create_task(
            contained(daemon.tab_groups.converge_all(), "tab group convergence")
        )
        # meiva:note
        # Chrome is confirmed up here — inject the gateway session cookie
        # right away (the periodic loop covers expiry; this covers the
        # fresh-profile cold start and the post-restart gap without
        # waiting for the next tick).
        gateway.spawn_inject()
        try:
            await await_death_or_stop(daemon.dead, stop)
        finally:
            watchdog.cancel()
            reaper.cancel()
            converge.cancel()


# meiva:note
# Process-entry wiring assembled from a booted ConfigBoot and captured LoadedCommit.
@dataclass(frozen=True)
class BrowserMcpEntryWiring:
    process_name: str
    config: ConfigBoot
    image: LoadedCommit
    process_home: Path
    machine: ConfiguredMachineIdentity
    browser_cdp_port: int
    default_ttl_seconds: float
    clock: Clock
    gateway_access: GatewayAccess

    def connect_timeout_seconds(self) -> float:
        return self.config.view.sandbox.mcp_connect_timeout_seconds


# meiva:note
# Assemble the process-entry collaborators from a booted ConfigBoot and captured LoadedCommit
# so `main()` wires telemetry and daemon runtime parameters from one owner.
def build_entry_wiring(
    *, config: ConfigBoot, image: LoadedCommit, home: Path
) -> BrowserMcpEntryWiring:
    machine = ConfiguredMachineIdentity(config)
    process_home = home.resolve()
    return BrowserMcpEntryWiring(
        process_name="browser-mcp",
        config=config,
        image=image,
        process_home=process_home,
        machine=machine,
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
        now=wiring.clock.now,
        otlp_enabled=lambda: wiring.config.view.observability.telemetry_otlp_enabled,
        otlp_endpoint=lambda: wiring.config.view.observability.telemetry_otlp_endpoint,
        database=None,
        machine=wiring.machine,
        home=wiring.process_home,
        rng=random.SystemRandom(),
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
            connect_timeout_reader=wiring.connect_timeout_seconds,
            clock=wiring.clock,
            gateway_access=wiring.gateway_access,
            default_ttl_seconds=wiring.default_ttl_seconds,
        )
    )


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
