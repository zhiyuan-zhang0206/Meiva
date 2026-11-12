# meiva:note
# Agent-owned page lifecycle: release on terminate + TTL sweep, across restarts.
#
# The browser-mcp daemon (``services.desktop.browser.mcp_daemon``) keys one Chrome page
# per agent (``PageRegistry.affinity``). A worker agent opens a tab on its first
# navigate; when the agent terminates it must not leave that tab (usually a dev
# server pointing at a dead localhost port) in the user's shared Chrome. Two
# mechanisms, both scoped to pages this stack created:
#
# - ``release_agent_page`` -- the deterministic close: the agent's process-exit
#   hook sends a ``release_agent_page`` wire request and the daemon closes every
#   page that agent created (wire handler ``handle_release_agent_page``).
# - ``expired_page_reaper`` / ``reap_expired_pages`` -- the hard deadline and the
#   only safety net (2026-09-11 ruling): every page this stack CREATES (an
#   explicit new_page, the auto-created page on a page-less first navigate, an
#   isolated-context page too) gets a record with a deadline of
#   ``now + MEIVA_CHROME_PAGE_DEFAULT_TTL_SECONDS`` (24h default), and the sweep
#   closes the page once that deadline passes. Activity never extends the
#   deadline; ``renew_agent_page`` (the ``renew_page`` tool) moves it to
#   ``now + ttl`` explicitly, at most 24h per call. It is the only cleanup for the
#   deaths that never reach the exit hook (SIGKILL / OOM / force-terminate) or a
#   dev server that died under a live agent.
#
# The records outlive the daemon: the daemon restarts on every cluster update and
# every upstream death while Chrome keeps running, so ``PageRegistry`` keeps one
# ``PageRecord`` per CDP target id in a ledger file (``tab_groups/page_store``),
# stable for the life of the Chrome process unlike chrome-devtools-mcp's page
# numbers. The pure decisions are in ``tab_groups/page_ledger`` (read its header
# for the rules); this module reads Chrome, closes targets and writes the file.
#
# The record is the one basis for closing. The ``Meiva #<id>`` tab group is the
# check that the user has not taken the page: a grouped page that left its owner's
# group is not closed and loses its record. A page in an agent group with no
# record is never closed (the group worker files tabs by URL on a layout restore,
# so a user's tab can be in one); only a Chrome-restart inheritance and the
# one-time migration give such a page a record.
#
# Safeguards on every close: the page is re-checked on a fresh read of Chrome right
# before ``/json/close`` (still there, still in its owner's group when grouped,
# not the browser's last page); the last-tab ungroup must succeed first; the close
# counts only when the target is really gone, else the record stays; a sweep
# closes a page only when two consecutive sweeps found it due, and the first sweep
# after a daemon start closes nothing (a transient wall-clock step cannot close a
# page); if Chrome's tabs cannot be read, nothing is closed.
#
# Pages are closed by CDP target id (``AgentTabGroups.close_target``), not through
# chrome-devtools-mcp's ``close_page``: its page ids mean nothing after a daemon
# restart, its output carries no target ids to translate them, and it refuses to
# close its last page.
#
# Affinity records selection, including tabs the agent did not create. A user tab
# the agent merely selected has no record and no renewal or automatic-close path.
# Page ids are only meaningful within the upstream process, so ``affinity`` and the
# page-id <-> target map never outlive the page numbering they record. What does
# outlive it is the agent's selection as a CDP target id (``selected_targets``, saved in
# ``<home>/run/browser-selected-pages.json``): the agent's first call after a daemon
# restart finds that target again in the new upstream's page list (``restored_page``)
# and re-selects it; a target that is gone, or whose URL does not single it out, is
# dropped. Restoring a selection grants nothing: closing rests on the records alone.

from __future__ import annotations

import asyncio
import math
import re
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from mcp import types

from base import telemetry
from base.log import logger
from services.desktop.browser.protocol import Response, WireRequest
from services.desktop.browser.tab_groups.page_ledger import (
    MAX_GRANT_SECONDS,
    ExpiredPage,
    Reconciled,
    TabState,
    agent_pages,
    is_taken,
    may_close,
    reconcile,
    target_of_url,
)
from services.desktop.browser.tab_groups.page_registry import PageRegistry as PageRegistry
from services.desktop.browser.tab_groups.page_selection import (
    listed_page_of_target,
    target_of_listed_page,
)
from services.desktop.browser.tab_groups.page_store import (
    FileRecordStore as FileRecordStore,
)
from services.desktop.browser.tab_groups.page_store import page_ledger_path

__all__ = ["FileRecordStore", "page_ledger_path"]

# meiva:note
# Expiry sweep cadence: a page is closed on the first pass after its TTL
# deadline, so it can outlive the deadline by up to one interval.
PAGE_SWEEP_INTERVAL_S = 600.0

# meiva:note
# How long after the daemon starts its first sweep (and so its first reconciliation
# of the ledger with Chrome's tabs) runs. After a Chrome restart the session
# restore re-applies the agents' tab groups in the background once Chrome answers;
# reading the groups before that would find the restored tabs ungrouped and leave
# their adoption to the next sweep. The read also holds the daemon's serial lock,
# which a daemon that has only just opened its socket should not spend on startup.
STARTUP_SWEEP_DELAY_S = 30.0

# meiva:note
# Hard cap on one renewal grant — at most 24h, the same cap persistent shell
# sessions live under (user ruling 2026-09-01). The initial (creation) deadline
# is the configured default, `chrome_page_default_ttl_seconds`.
DEFAULT_PAGE_TTL_SECONDS = 86_400.0
_MAX_TTL_SECONDS = MAX_GRANT_SECONDS

# meiva:note
# The ``renew_page`` tool this module owns. Lives beside the TTL machinery
# because the daemon appends it to the upstream tool list verbatim passthrough
# (``chrome-devtools-mcp`` has no TTL concept of its own).
RENEW_PAGE_TOOL = "renew_page"

_RENEW_PAGE_DESCRIPTION = (
    "Renew the TTL deadline of this agent's current page. Pages opened through "
    "this browser carry a hard deadline (24h default) after which they are "
    "closed; renewal moves the deadline to now + ttl (default: the configured "
    "page TTL)."
)


def renew_page_tool_dump() -> dict[str, object]:
    """The daemon-owned ``renew_page`` tool dict, in the same wire shape as the
    upstream tools the daemon passes through verbatim (``types.Tool`` dump):
    appended to ``list_tools`` by ``ChromeMcpDaemon``."""
    return types.Tool(
        name=RENEW_PAGE_TOOL,
        description=_RENEW_PAGE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "ttl": {
                    "type": "number",
                    "description": (
                        "Seconds from now until the new deadline. Defaults to the "
                        "cluster's default page TTL; must be greater than zero and "
                        "at most 86400 (24 hours)."
                    ),
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    ).model_dump(mode="json", by_alias=True)


# meiva:note
# Page-list line shapes, mirroring chrome-devtools-mcp's renderer: an untitled
# page is `  <id>: <url>`, a titled one `  <id>: <title> (<url>)`; either may
# end in ` [selected]` and zero or more ` key=value` suffixes (an isolated-
# context tab appends ` isolatedContext=<name>`). The URL is the bare second
# token, or the last `(…)` group of a titled label -- a URL that itself
# contains parens stays whole for the untitled shape.
_PAGE_LINE_RE = re.compile(
    r"^\s*(\d+):\s*(?:.+\s\((\S+)\)|(\S+))(?:\s+\[selected\])?(?:\s+\S+=\S+)*\s*$"
)


# meiva:note
# The slice of ``AgentTabGroups`` the lifecycle reads and closes with.
class _TabGroups(Protocol):
    async def tab_state(self) -> TabState: ...

    async def close_target(self, agent_id: int, target_id: str, *, grouped: bool) -> bool: ...


class _PageDaemon(Protocol):
    """The slice of ``ChromeMcpDaemon`` the page-lifecycle helpers use.

    A structural protocol keeps this module free of a circular import with the
    daemon (which imports this module for the reaper + wire handling) while
    staying pyright-clean.
    """

    _lock: asyncio.Lock
    pages: PageRegistry

    @property
    def tab_groups(self) -> _TabGroups: ...

    async def _call(self, name: str, args: dict[str, object]) -> types.CallToolResult: ...

    async def call_tool(
        self, name: str, args: dict[str, object], current_page: int | None
    ) -> tuple[types.CallToolResult, int | None]: ...


def text_of(result: types.CallToolResult) -> str:
    return "".join(c.text for c in result.content if isinstance(c, types.TextContent))


async def forward_legacy_call(
    daemon: _PageDaemon,
    tool: str,
    args: dict[str, object],
    conn_page: int | None,
) -> tuple[types.CallToolResult, int | None]:
    """Forward one agent-less call with the connection's own page.

    Returns the result and the connection's updated current page.
    """
    return await daemon.call_tool(tool, args, conn_page)


# meiva:note
# Read Chrome's tabs, reconcile the records with them and persist the result.
# Callers hold the daemon's serial lock. None when Chrome's tabs cannot be read:
# the caller then closes nothing (a WARNING names the failure).
async def _reconciled(daemon: _PageDaemon) -> tuple[Reconciled, TabState] | None:
    try:
        tabs = await daemon.tab_groups.tab_state()
    except Exception as e:
        logger.opt(exception=not isinstance(e, TimeoutError)).warning(
            "[browser-mcp] could not read Chrome's tabs and tab groups ({}: {}); closing no page "
            "in this pass",
            type(e).__name__,
            e,
        )
        return None
    result = reconcile(
        daemon.pages.records,
        tabs,
        now=daemon.pages.now(),
        default_ttl_seconds=daemon.pages.default_ttl_seconds,
        migrate=daemon.pages.migrate_pending,
    )
    daemon.pages.replace_state(result.records, migration_ran=result.migrated)
    _log_reconciled(result, tabs, daemon.pages.default_ttl_seconds)
    return result, tabs


# meiva:note
# An origin without path or query, for logs: enough to recognise a page, nothing
# a log should carry.
def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}"


def _log_reconciled(result: Reconciled, tabs: TabState, default_ttl_seconds: float) -> None:
    for target_id, agent_id in result.adopted:
        logger.info(
            f"[browser-mcp] page ledger migration adopted page {target_id} of agent {agent_id} "
            f"({_origin(tabs.pages[target_id])}) with a {default_ttl_seconds:g}s TTL"
        )
    for old_id, new_id, agent_id in result.inherited:
        logger.info(
            f"[browser-mcp] page ledger passed the record of gone page {old_id} to restored page "
            f"{new_id} of agent {agent_id} ({_origin(tabs.pages[new_id])})"
        )
    for target_id in result.taken:
        logger.info(
            f"[browser-mcp] page {target_id} left its agent's tab group (taken by the user); "
            "dropping its record, the page stays open"
        )
    if result.vanished:
        logger.info(
            f"[browser-mcp] page ledger dropped {len(result.vanished)} record(s) of pages "
            "Chrome no longer has"
        )
    if result.unrecorded:
        logger.info(
            f"[browser-mcp] {result.unrecorded} page(s) sit in an agent tab group with no ledger "
            "record; they are neither adopted nor closed"
        )


# meiva:note
# The re-read of Chrome's tabs before a close failed: the caller ends its pass.
class _ChromeUnreadableError(Exception):
    pass


# meiva:note
# Close one recorded page; False when it stays open. The page is checked once more
# on a fresh read of Chrome just before the close (`may_close`: still there, not taken
# by the user, not the browser's last page); a page taken since the reconciliation
# loses its record instead. When that read fails the pass is abandoned
# (`_ChromeUnreadableError`) rather than waiting out one timeout per page under the
# serial lock. A close
# counts only when the target is gone, else the record stays and the next pass tries
# again; repeated failures of one target are logged on the first and then every sixth
# time, without a traceback. Callers hold the serial lock.
async def _close(daemon: _PageDaemon, page: ExpiredPage) -> bool:
    record = daemon.pages.records.get(page.target_id)
    if record is None:
        return False
    try:
        fresh = await daemon.tab_groups.tab_state()
    except Exception as e:
        logger.opt(exception=not isinstance(e, TimeoutError)).warning(
            "[browser-mcp] could not re-read Chrome's tabs before closing page {} ({}: {}); "
            "ending this pass",
            page.target_id,
            type(e).__name__,
            e,
        )
        raise _ChromeUnreadableError from e
    if is_taken(record, page.target_id, fresh):
        logger.info(
            f"[browser-mcp] page {page.target_id} is in a tab group that is not its owner's "
            "(taken by the user); dropping its record, the page stays open"
        )
        daemon.pages.forget_target(page.target_id)
        return False
    if not may_close(record, page.target_id, fresh):
        return False
    try:
        gone = await daemon.tab_groups.close_target(
            page.owner, page.target_id, grouped=record.grouped
        )
        failure = None if gone else "the target is still open after the close"
    except Exception as e:
        failure = f"{type(e).__name__}: {e}"
    if failure is not None:
        count = daemon.pages.close_failures.get(page.target_id, 0) + 1
        daemon.pages.close_failures[page.target_id] = count
        if count == 1 or count % 6 == 0:
            logger.warning(
                f"[browser-mcp] could not close page {page.target_id} ({_origin(page.url)}) of "
                f"agent {page.owner} ({failure}); it stays recorded and is retried (failure {count})"
            )
        return False
    page_id = daemon.pages.page_of_target(page.target_id)
    if page_id is not None:
        daemon.pages.clear_affinity_for_page(page_id)
    daemon.pages.forget_target(page.target_id)
    return True


# meiva:note
# Drop affinity, closing every page this agent created. Called when the agent
# terminates (wire method `release_agent_page`, sent by the termination cleanup
# hook). Idempotent. A borrowed page is deselected without closing; every recorded
# page owned by `agent_id` goes through `_close`, including pages created before a
# daemon restart. The returned id is the closed current page (or the last closed page
# this process knew the id of), or None. When Chrome's tabs cannot be read nothing is
# closed and the affinity stays; the expiry sweep owns what is left.
async def release_agent_page(daemon: _PageDaemon, agent_id: int) -> int | None:
    async with daemon._lock:
        current_id = daemon.pages.get_agent_page(agent_id)
        refreshed = await _reconciled(daemon)
        if refreshed is None:
            return None
        closed_ids: list[int] = []
        for page in agent_pages(daemon.pages.records, refreshed[1], agent_id):
            page_id = daemon.pages.page_of_target(page.target_id)
            try:
                closed = await _close(daemon, page)
            except _ChromeUnreadableError:
                break
            if closed and page_id is not None:
                closed_ids.append(page_id)
        daemon.pages.set_agent_page(agent_id, None)
        if current_id in closed_ids:
            return current_id
        return closed_ids[-1] if closed_ids else None


async def handle_release_agent_page(
    daemon: _PageDaemon, req: WireRequest, req_id: int | None
) -> Response:
    """Release affinity, closing created pages, on a ``release_agent_page`` request.

    The request is a terminated agent's exit hook telling the service it is
    done with the browser; the reply names the page that was closed (None for
    a borrowed page or no affinity). A missing/aliased agent id is rejected at the protocol
    edge — same guard as the daemon's ``call_tool``, so a JSON ``true`` can
    never alias another agent's slot.
    """
    agent_id = req.get("agent_id")
    if isinstance(agent_id, int) and not isinstance(agent_id, bool):
        page_id = await release_agent_page(daemon, agent_id)
        return {"id": req_id, "ok": True, "result": {"page_id": page_id}}
    return {
        "id": req_id,
        "ok": False,
        "error": "release_agent_page requires a valid 'agent_id'",
    }


# meiva:note
# The agent's current page. The first call of a daemon process for an agent restores the
# page it had selected before the restart: its saved target is looked up in Chrome and in
# the new upstream's listing, and re-selected only when both name exactly one tab with that
# URL. Anything else (the tab is gone, an ambiguous URL, a listing that cannot be read)
# drops the selection and the agent starts page-less, as before. Callers hold the serial lock.
async def restored_page(daemon: _PageDaemon, agent_id: int) -> int | None:
    pages = daemon.pages
    if agent_id in pages.affinity:
        return pages.affinity[agent_id]
    target_id = pages.selected_targets.get(agent_id)
    page_id = await _restore_selection(daemon, agent_id, target_id) if target_id else None
    pages.set_agent_page(agent_id, page_id)
    return page_id


# meiva:note
# Chrome's page targets with their URLs, or None when its tabs cannot be read (a WARNING says
# so): the saved selection is a convenience and never worth failing a call for.
async def _chrome_urls(daemon: _PageDaemon, what: str) -> dict[str, str] | None:
    try:
        return dict((await daemon.tab_groups.tab_state()).pages)
    except Exception as e:
        logger.warning(f"[browser-mcp] could not read Chrome's tabs to {what} ({e!r})")
        return None


async def _restore_selection(daemon: _PageDaemon, agent_id: int, target_id: str) -> int | None:
    listing = await daemon._call("list_pages", {})
    urls = await _chrome_urls(daemon, f"restore agent {agent_id}'s selected page")
    page_id = (
        None
        if listing.is_error or urls is None
        else listed_page_of_target(parse_page_listing(text_of(listing)), urls, target_id)
    )
    if page_id is None:
        logger.info(
            f"[browser-mcp] agent {agent_id}'s selected page {target_id} is gone or not "
            "identifiable after the restart; the selection is dropped"
        )
        return None
    daemon.pages.page_targets[page_id] = target_id
    logger.info(f"[browser-mcp] agent {agent_id}'s selected page {target_id} restored as {page_id}")
    return page_id


# meiva:note
# Save the target of the page ``agent_id`` now has selected (a page that changed through
# this call) so a daemon restart can restore it. A page this process created or matched
# has its target; any other (a tab the agent selected) is matched by its URL against
# Chrome's tabs, ``result``'s listing first. A page that cannot be identified leaves
# nothing saved: no stale target outlives its page. Never fails the call it follows.
async def remember_selection(
    daemon: _PageDaemon, agent_id: int, page_id: int | None, result: types.CallToolResult
) -> None:
    if page_id is None:
        return
    target_id = daemon.pages.page_targets.get(page_id)
    if target_id is None:
        try:
            listing = parse_page_listing(text_of(result))
            if page_id not in listing:
                listing = parse_page_listing(text_of(await daemon._call("list_pages", {})))
            urls = await _chrome_urls(daemon, f"save agent {agent_id}'s selected page")
            target_id = None if urls is None else target_of_listed_page(listing, urls, page_id)
        except Exception as e:
            logger.warning(
                f"[browser-mcp] could not list the pages to save agent {agent_id}'s selection ({e!r})"
            )
    daemon.pages.set_selected_target(agent_id, target_id)


def parse_page_listing(text: str) -> dict[int, str]:
    """page id -> URL from a list_pages / new_page listing result.

    Best-recovery parse: a line that drifts off the shape is skipped; an empty
    dict means nothing matched and the caller treats that as no candidates.
    """
    pages: dict[int, str] = {}
    for line in text.splitlines():
        m = _PAGE_LINE_RE.match(line)
        if m:
            # meiva:note
            # Titled label -> the `(…)` group; untitled -> the bare token.
            pages[int(m.group(1))] = m.group(2) or m.group(3)
    return pages


def _tool_ok(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=False)


def _tool_error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


# meiva:note
# The target of an agent's current page, or None when it is not provably one of its own.
#
# A page this process created or already matched is known. After a daemon
# restart the upstream's page numbers are new: the page is then matched by URL
# against the agent's own recorded pages, and only an unambiguous match counts.
async def _target_of_page(daemon: _PageDaemon, agent_id: int, page_id: int) -> str | None:
    known = daemon.pages.page_targets.get(page_id)
    if known is not None:
        return known
    listing = await daemon._call("list_pages", {})
    url = None if listing.is_error else parse_page_listing(text_of(listing)).get(page_id)
    if url is None:
        return None
    try:
        tabs = await daemon.tab_groups.tab_state()
    except Exception as e:
        logger.warning(
            f"[browser-mcp] could not read Chrome's tabs to find agent {agent_id}'s page "
            f"{page_id} ({type(e).__name__}: {e})"
        )
        return None
    target_id = target_of_url(daemon.pages.records, tabs, agent_id, url)
    if target_id is not None:
        daemon.pages.page_targets[page_id] = target_id
    return target_id


# meiva:note
# Handle one ``renew_page`` tool call: move the agent page's TTL deadline.
#
# Semantics mirror ``meiva.shell.sessions.renew`` (the shell-TTL ruling): the
# deadline becomes ``now + ttl`` — never stacked on the old one — each call
# grants at most 24h, and an already-passed deadline is NOT renewable (the
# sweep owns the page; there is no renewable expired state). ``ttl`` is
# optional and defaults to the configured page TTL. Renewal targets the
# CALLER's current page: a page without a record has no TTL and no renewal
# path, so the user's tabs are out of scope by construction. It works on a
# page created before a daemon restart too (see ``_target_of_page``).
# Runs under the serial lock, so a renewal and the expiry sweep can never
# interleave — a successful renewal proves the deadline had not passed.
async def renew_agent_page(
    daemon: _PageDaemon, agent_id: int, args: dict[str, object]
) -> types.CallToolResult:
    unknown = set(args) - {"ttl"}
    if unknown:
        return _tool_error(f"renew_page got unexpected argument(s): {sorted(unknown)}")
    raw = args.get("ttl")
    if raw is None:
        ttl = daemon.pages.default_ttl_seconds
    elif (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or raw <= 0
    ):
        return _tool_error("ttl must be a finite number of seconds greater than zero")
    else:
        ttl = float(raw)
    if ttl > _MAX_TTL_SECONDS:
        return _tool_error(f"ttl must be at most {_MAX_TTL_SECONDS:.0f} seconds (24 hours)")
    async with daemon._lock:
        page_id = await restored_page(daemon, agent_id)
        if page_id is None:
            return _tool_error("this agent has no current page to renew; open one first")
        target_id = await _target_of_page(daemon, agent_id, page_id)
        record = daemon.pages.records.get(target_id) if target_id is not None else None
        if target_id is None or record is None:
            return _tool_error(
                f"page {page_id} has no page TTL to renew (not created through this "
                "browser, or already closed); open a new page"
            )
        now = daemon.pages.now()
        if now >= record.expires_at:
            return _tool_error(
                f"page {page_id} is past its TTL deadline and cannot be renewed; open a new page"
            )
        daemon.pages.set_deadline(target_id, now + ttl)
        new_expires = datetime.fromtimestamp(now + ttl, tz=UTC)
        telemetry.emit(
            "telemetry",
            "chrome_page_ttl_renewed",
            level="info",
            agent_id=agent_id,
            attributes={
                "page_id": page_id,
                "target_id": target_id,
                # meiva:note
                # Same-class cast as task #4011 (stable emitted metric kind).
                "ttl_s": round(ttl),
                "new_expires_at": new_expires.isoformat(),
            },
        )
        logger.info(f"[browser-mcp] agent {agent_id} renewed page {page_id} TTL to {ttl:g}s")
        return _tool_ok(
            f"Page {page_id} TTL renewed: extends to {new_expires.isoformat()} ({ttl:g}s from now)."
        )


# meiva:note
# Close recorded pages whose TTL deadline has passed. The pass reconciles the ledger
# with Chrome first (`page_ledger.reconcile`: the one-time migration, restart
# inheritance, pages the user took, the due list), then closes each page that this
# sweep AND the previous one found due, through `_close` (fresh re-check, by target
# id). A sweep that is the first since the daemon started closes nothing: a transient
# wall-clock step forward cannot close a page. The whole pass runs under the serial
# lock, so it cannot interleave with a renewal or a release. Expiry is surfaced to
# the agent only by the page being gone: the next page-scoped call hits the daemon's
# existing no-page path. Returns the number of pages closed.
async def reap_expired_pages(daemon: _PageDaemon) -> int:
    async with daemon._lock:
        refreshed = await _reconciled(daemon)
        if refreshed is None:
            # meiva:note
            # a pass that could not read Chrome breaks the run
            daemon.pages.due_seen = set()
            return 0
        due = refreshed[0].due
        confirmed = [page for page in due if page.target_id in daemon.pages.due_seen]
        daemon.pages.due_seen = {page.target_id for page in due}
        closed = 0
        for page in confirmed:
            page_id = daemon.pages.page_of_target(page.target_id)
            try:
                if not await _close(daemon, page):
                    continue
            except _ChromeUnreadableError:
                daemon.pages.due_seen = set()
                return closed
            telemetry.emit(
                "log",
                "chrome_page_ttl_expired",
                level="info",
                agent_id=page.owner,
                attributes={
                    "page_id": page_id,
                    "target_id": page.target_id,
                    "url": page.url,
                    "agent_id": page.owner,
                },
            )
            closed += 1
            logger.info(
                f"[browser-mcp] TTL sweep closed page {page.target_id} ({_origin(page.url)}) "
                f"for agent {page.owner}"
            )
        return closed


# meiva:note
# Close pages past their hard TTL deadline: the first pass ``startup_delay_s``
# after the daemon starts (a restart is when the ledger is reconciled with
# Chrome's tabs), then one every interval (see ``reap_expired_pages`` — up to
# one interval of lag on a deadline). Runs for the daemon's upstream connection
# like its watchdog.
async def expired_page_reaper(
    daemon: _PageDaemon,
    stop: asyncio.Event,
    *,
    sweep_interval_s: float = PAGE_SWEEP_INTERVAL_S,
    startup_delay_s: float = STARTUP_SWEEP_DELAY_S,
) -> None:
    delay = startup_delay_s
    while True:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        if stop.is_set():
            return
        try:
            await reap_expired_pages(daemon)
        except Exception as e:
            # meiva:note
            # One bad pass must not kill the reaper.
            logger.warning(f"[browser-mcp] expired-page reaper pass failed: {e!r}")
        delay = sweep_interval_s
