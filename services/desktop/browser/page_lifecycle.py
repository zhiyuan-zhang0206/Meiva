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
#   only safety net (2026-09-11 ruling): every page this stack CREATED (an
#   explicit new_page, or the auto-created page on a page-less first navigate)
#   gets a record with a deadline of ``now + MEIVA_CHROME_PAGE_DEFAULT_TTL_SECONDS``
#   (24h default), and the sweep closes the page once that deadline passes.
#   Activity never extends the deadline; ``renew_agent_page`` (the ``renew_page``
#   tool) moves it to ``now + ttl`` explicitly, at most 24h per call. It bounds a
#   page an agent keeps USING across days, and it is the only cleanup for the
#   deaths that never reach the exit hook (SIGKILL / OOM / force-terminate) or a
#   dev server that died under a live agent.
#
# The records outlive the daemon. The daemon restarts often (every cluster update,
# every upstream ``chrome-devtools-mcp`` death) while Chrome keeps running, and a
# deadline held only in the daemon's memory was lost each time, leaving the tab
# with no record at all. So ``PageRegistry`` keeps one ``PageRecord`` per page
# target in a ledger file (``<home>/run/browser-pages.json``), keyed by the CDP
# target id -- stable for the life of the Chrome process, unlike
# chrome-devtools-mcp's page numbers, which restart with it. The pure decisions
# (reconciliation, which pages are due, which belong to an agent) are in
# ``page_ledger``; this module reads Chrome, closes targets and writes the file.
#
# Every pass (the sweep, and a release) first RECONCILES the ledger with Chrome's
# tabs: records of vanished targets are dropped, and a page in a ``Meiva #<id>``
# tab group that has no record -- left by a Chrome restart (new target ids), by a
# session restore, or by a version before the ledger existed -- is ADOPTED for the
# agent the title names, with a fresh default TTL. The worst case for an unrecorded
# page is therefore one extra TTL, not forever. The one safety rule: a page is
# closed only on positive evidence that it is ours, namely that Chrome shows it in
# an agent's tab group right now; a page in no such group is never touched, and
# if Chrome's tabs cannot be read nothing is closed and a warning says so.
#
# Pages are closed by CDP target id (``AgentTabGroups.close_target``), not through
# chrome-devtools-mcp's ``close_page``: its page ids mean nothing after a daemon
# restart, its output carries no target ids to translate them, and it refuses to
# close its last page. Closing the target is also what a user closing the tab
# does, which the upstream already tolerates.
#
# Affinity records selection, including tabs the agent did not create. The
# ledger record also proves creation: automatic cleanup requires it even when a
# tab has affinity. A user tab the agent merely selected has no record and no
# renewal or automatic-close path.
# The registry lives in one ``PageRegistry`` the daemon's composition root
# (``mcp_daemon.run``) builds for the daemon. Page ids are only meaningful within
# the upstream process (chrome-devtools-mcp numbers pages per process), and an
# upstream death ends the daemon, so ``affinity`` and the page-id <-> target map
# never outlive the page numbering they record; the ledger does.


from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from mcp import types

from base import telemetry
from base.clock import Clock
from base.host.atomic_io import write_text_atomic
from base.log import logger
from base.paths import run_dir
from services.desktop.browser.protocol import Response, WireRequest
from services.desktop.browser.tab_groups.page_ledger import (
    MAX_GRANT_SECONDS,
    ExpiredPage,
    PageRecord,
    Reconciled,
    TabState,
    agent_pages,
    dump_records,
    parse_records,
    reconcile,
    target_of_url,
)

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
# Where the page records live between daemon processes.
class RecordStore(Protocol):
    def load(self) -> dict[str, PageRecord]: ...

    def save(self, records: Mapping[str, PageRecord]) -> None: ...


# meiva:note
# The page ledger file of a home (`<home>/run/browser-pages.json`), beside the
# browser session ledger (`browser-session.json`): runtime state that survives a
# daemon restart and is read by no other process.
def page_ledger_path(home: Path) -> Path:
    return run_dir(home) / "browser-pages.json"


# meiva:note
# The page records as one JSON file, replaced atomically.
#
# A file that cannot be read or parsed is not an error: it reads as no records
# (a WARNING says so), and the next reconciliation adopts the pages from their
# tab groups. A failed save is a WARNING too -- the in-memory records stay
# right, and the worst outcome is the same adoption after the next restart.
class FileRecordStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, PageRecord]:
        try:
            return parse_records(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning(
                "[browser-mcp] ignoring unreadable page ledger {} ({}: {}); the pages in agent "
                "tab groups will be adopted with a fresh TTL",
                self._path,
                type(e).__name__,
                e,
            )
            return {}

    def save(self, records: Mapping[str, PageRecord]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(self._path, dump_records(records), mode=0o600)
        except OSError as e:
            logger.warning(
                "[browser-mcp] could not write the page ledger {} ({}: {}); deadlines are kept "
                "in memory and the pages are adopted again after a restart",
                self._path,
                type(e).__name__,
                e,
            )


# meiva:note
# The page-keyed state of the shared browser-mcp daemon, one per process.
#
# Built once by the daemon's composition root and handed to its
# ``ChromeMcpDaemon``. The daemon serves one upstream connection and exits
# when it dies, so every page id here belongs to the upstream process that is
# still serving.
#
# - ``affinity``: agent id -> current page id. The selected-page state
#   belongs to the AGENT, not to a TCP connection: an exec subprocess child
#   re-connecting mid-turn (or the agent process itself after a session
#   rebuild) must land on the same tab the agent selected, not cold-start
#   with "No page selected" on every exec. One entry per agent that has used
#   the browser — bounded by the machine's agent count; a closed/crashed page
#   drops the slot naturally via the existing re-pin failure path. Requests
#   without an agent id (legacy wrapper clients) keep the per-connection
#   fallback in the daemon.
# - ``records``: CDP target id -> ``PageRecord`` (creating agent, deadline in
#   wall-clock epoch seconds). Every page THIS STACK created and identified
#   (an explicit new_page call, or the auto-created page on a page-less first
#   navigate) gets one from ``register_created_page``, and reconciliation
#   adopts the pages in agent tab groups that lack one. It is the ONLY
#   candidate source for closing, so pages this stack never created (the
#   user's own tabs) are never inspected or closed. The records are persisted
#   on every change (see ``RecordStore``). Wall clock, not monotonic: a
#   monotonic deadline cannot survive the process, which is the point of the
#   ledger; ``page_ledger`` documents how a clock step is contained.
# - ``page_targets``: page id -> target id, for the pages this process created
#   or has matched to a target. Page ids are chrome-devtools-mcp's, so this
#   starts empty with every daemon process.
class PageRegistry:
    def __init__(
        self,
        default_ttl_seconds: float = DEFAULT_PAGE_TTL_SECONDS,
        *,
        clock: Clock,
        store: RecordStore,
    ) -> None:
        self.default_ttl_seconds = float(default_ttl_seconds)
        self._clock = clock
        self._store = store
        self.affinity: dict[int, int | None] = {}
        self.records: dict[str, PageRecord] = store.load()
        self.page_targets: dict[int, str] = {}

    # meiva:note
    # Wall-clock epoch seconds, the unit of every deadline.
    def now(self) -> float:
        return self._clock.now().timestamp()

    def get_agent_page(self, agent_id: int) -> int | None:
        """The agent's current page, or None when it has none."""
        return self.affinity.get(agent_id)

    def set_agent_page(self, agent_id: int, page_id: int | None) -> None:
        """Record the agent's current page."""
        self.affinity[agent_id] = page_id

    # meiva:note
    # Give a freshly created page its record: the owner and ``now + default TTL``.
    #
    # Called by the daemon right after a ``new_page`` it forwarded and the tab
    # grouping identified the page's target. A page whose target was not
    # identified (no agent, isolated context, grouping could not find it)
    # carries no record: it has no stable identity and no tab group to prove
    # it ours, so it has no TTL.
    def register_created_page(
        self, target_id: str | None, agent_id: int | None, page_id: int | None
    ) -> None:
        if target_id is None or agent_id is None:
            return
        self.records[target_id] = PageRecord(agent_id, self.now() + self.default_ttl_seconds)
        if page_id is not None:
            self.page_targets[page_id] = target_id
        self._store.save(self.records)

    # meiva:note
    # The record of a page this process knows the target of.
    def record_for_page(self, page_id: int) -> PageRecord | None:
        target_id = self.page_targets.get(page_id)
        return self.records.get(target_id) if target_id is not None else None

    def page_of_target(self, target_id: str) -> int | None:
        for page_id, known in self.page_targets.items():
            if known == target_id:
                return page_id
        return None

    # meiva:note
    # Adopt a reconciled record set, writing the file only when it changed.
    def replace_records(self, records: dict[str, PageRecord]) -> None:
        if records != self.records:
            self.records = records
            self._store.save(records)

    # meiva:note
    # Move one record's deadline (the owner stays).
    def set_deadline(self, target_id: str, expires_at: float) -> None:
        self.records[target_id] = PageRecord(self.records[target_id].owner, expires_at)
        self._store.save(self.records)

    # meiva:note
    # Drop the record of a page that is closed (or was never ours).
    def forget_target(self, target_id: str) -> None:
        page_id = self.page_of_target(target_id)
        if page_id is not None:
            del self.page_targets[page_id]
        if self.records.pop(target_id, None) is not None:
            self._store.save(self.records)

    # meiva:note
    # Forget the record of a page closed through the upstream's ``close_page``.
    def drop_page_ttl(self, page_id: int | None) -> None:
        target_id = self.page_targets.get(page_id) if page_id is not None else None
        if target_id is not None:
            self.forget_target(target_id)

    # meiva:note
    # Clear every agent slot naming ``page_id`` (the page is gone);
    #
    # returns one cleared affinity owner for logs (None when no slot named
    # the page). Every slot is cleared — a slot left naming a closed page
    # would make the owner's next call re-pin against a dead id first.
    def clear_affinity_for_page(self, page_id: int) -> int | None:
        owner: int | None = None
        for agent_id, current in list(self.affinity.items()):
            if current == page_id:
                self.set_agent_page(agent_id, None)
                if owner is None:
                    owner = agent_id
        return owner


# meiva:note
# The slice of ``AgentTabGroups`` the lifecycle reads and closes with.
class _TabGroups(Protocol):
    async def tab_state(self) -> TabState: ...

    async def close_target(self, agent_id: int, target_id: str) -> None: ...


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
#
# Callers hold the daemon's serial lock. None when Chrome's tabs cannot be read:
# the caller then closes nothing, because without them no page can be proven
# ours. A WARNING names the failure.
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
    )
    daemon.pages.replace_records(result.records)
    _log_reconciled(result, tabs, daemon.pages.default_ttl_seconds)
    return result, tabs


def _log_reconciled(result: Reconciled, tabs: TabState, default_ttl_seconds: float) -> None:
    if result.adopted:
        by_agent: dict[int, int] = {}
        for _, agent_id in result.adopted:
            by_agent[agent_id] = by_agent.get(agent_id, 0) + 1
        per_agent = ", ".join(f"agent {a}: {n}" for a, n in sorted(by_agent.items()))
        logger.info(
            f"[browser-mcp] page ledger adopted {len(result.adopted)} page(s) from agent tab "
            f"groups with a {default_ttl_seconds:g}s TTL ({per_agent})"
        )
    if result.vanished:
        logger.info(
            f"[browser-mcp] page ledger dropped {len(result.vanished)} record(s) of pages "
            "Chrome no longer has"
        )
    for target_id in result.lapsed:
        logger.warning(
            f"[browser-mcp] page {target_id} ({tabs.pages.get(target_id, '')}) is past its "
            "deadline but is in no agent tab group; leaving it open and dropping its record"
        )


# meiva:note
# Close one page by target id and forget it; False (with a WARNING) when Chrome refused.
#
# A failed close keeps the record, so the next pass tries again. Callers hold
# the serial lock.
async def _close(daemon: _PageDaemon, page: ExpiredPage) -> bool:
    try:
        await daemon.tab_groups.close_target(page.owner, page.target_id)
    except Exception as e:
        logger.opt(exception=not isinstance(e, TimeoutError)).warning(
            "[browser-mcp] could not close page {} ({}) of agent {} ({}: {}); it stays recorded "
            "and is retried on the next pass",
            page.target_id,
            page.url,
            page.owner,
            type(e).__name__,
            e,
        )
        return False
    page_id = daemon.pages.page_of_target(page.target_id)
    if page_id is not None:
        daemon.pages.clear_affinity_for_page(page_id)
    daemon.pages.forget_target(page.target_id)
    return True


# meiva:note
# Drop affinity, closing every page this agent created.
#
# Called when the agent terminates (wire method ``release_agent_page``,
# sent by the termination cleanup hook). Idempotent: an agent with no
# created pages or affinity is a no-op. A borrowed page is deselected
# without closing; every page owned by ``agent_id`` that Chrome shows in its
# tab group is closed so the agent's group disappears with its last tab --
# including pages created before a daemon restart. The returned id is the
# closed current page (or the last closed page this process knew the id of),
# or None. When Chrome's tabs cannot be read nothing is closed and the
# affinity stays; the expiry sweep owns what is left.
async def release_agent_page(daemon: _PageDaemon, agent_id: int) -> int | None:
    async with daemon._lock:
        current_id = daemon.pages.get_agent_page(agent_id)
        refreshed = await _reconciled(daemon)
        if refreshed is None:
            return None
        closed_ids: list[int] = []
        for page in agent_pages(daemon.pages.records, refreshed[1], agent_id):
            page_id = daemon.pages.page_of_target(page.target_id)
            if await _close(daemon, page) and page_id is not None:
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
        page_id = daemon.pages.get_agent_page(agent_id)
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
# Close stack-created pages whose TTL deadline has passed.
#
# The pass reconciles the ledger with Chrome first (``page_ledger.reconcile``:
# adoption, dropping vanished targets, the due list), then closes each due
# page by target id. Only pages with a record, shown by Chrome in an agent tab
# group right now, are candidates, so the user's tabs are never touched; a due
# record whose page is in no agent group is dropped and the page left open
# (WARNING). The whole pass runs under the serial lock, so it cannot
# interleave with a renewal or a release: a renewed page is never closed off a
# stale read. A close that Chrome refuses keeps its record and is retried on
# the next pass. Expiry is surfaced to the agent only by the page being
# gone: the next page-scoped call hits the daemon's existing no-page path —
# no invented "page expired" error.
# Returns the number of pages closed.
async def reap_expired_pages(daemon: _PageDaemon) -> int:
    async with daemon._lock:
        refreshed = await _reconciled(daemon)
        if refreshed is None:
            return 0
        closed = 0
        for page in refreshed[0].to_close:
            page_id = daemon.pages.page_of_target(page.target_id)
            if not await _close(daemon, page):
                continue
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
                f"[browser-mcp] TTL sweep closed page {page.target_id} ({page.url}) "
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
