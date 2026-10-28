# meiva:note
# One Chrome tab group per agent, for the pages that agent's browser calls create.
#
# Chrome's DevTools protocol has no tab-group API (measured on Chrome 153: none
# of its domains mentions groups), so grouping goes through a small extension
# shipped in ``extension/``. The browser-mcp daemon loads it with
# ``Extensions.loadUnpacked`` -- available on the ordinary
# ``--remote-debugging-port`` connection, no launch flag -- and calls the
# ``meivaGroup(targetId, title)`` function its service worker exposes.
#
# The invariant is stated once, here: an agent's pages sit in exactly one tab
# group per window (Chrome groups cannot span windows, so "one group per agent" is
# only ever true per window; no tab is ever moved to another window to satisfy a
# title). Every entry point CONVERGES rather than assumes: it re-reads Chrome's
# open groups by title, keeps one deterministic group per (title, window), and
# moves the other groups' tabs into it. Convergence is a function of Chrome's state
# alone -- nothing is remembered -- so a daemon restart, a Chrome restart, an
# extension update and a repeated run all land in the same place, and the second
# run of any of them changes nothing.
#
# The facts that shape the design:
#
# - **Loading is per Chrome session and the worker idles out.** An unpacked
#   extension loaded over CDP is gone after a Chrome restart, and its service
#   worker stops ~30s after its last event (its CDP target disappears). Each
#   grouping therefore checks, on the same websocket, whether the extension is
#   loaded with a live worker of the file name this checkout ships, and issues
#   the idempotent ``Extensions.loadUnpacked`` only when it is not (Chrome
#   restarted, worker idled out, or an older worker file is still running). It
#   must not reload a live worker: the reload tears the worker down, and a
#   call that attached to the dying target never gets its reply (measured).
#   After attaching it waits for the worker script to have defined ``meivaGroup``
#   (the target can be attachable first). Nothing is remembered between calls,
#   so daemon restarts need no repair.
# - **Page ids are not target ids.** The daemon's page ids are chrome-devtools-mcp
#   sequence numbers; the extension needs the CDP targetId. The caller holds the
#   daemon's machine-wide serial lock, so the one page target that appears
#   between ``snapshot()`` (before ``new_page``) and ``group_new_page()`` (after)
#   is the page that was just created. Anything other than exactly one new
#   target (a user opening a tab meanwhile, a popup) is reported and left
#   ungrouped -- the module never guesses which tab is the agent's.
# - **Only the two target listings run under that lock.** They are short
#   (``LISTING_TIMEOUT_S``). Everything else -- loading the extension, attaching
#   to its worker, calling it -- runs in a background task with its own timeout
#   (``GROUP_TIMEOUT_S``), so a slow or wedged Chrome costs the page creation
#   nothing. The tasks run one at a time (``_lock``, the grouping's own lock, not
#   the daemon's): two grouping tasks of one agent both search the group by title
#   and would otherwise each create it. ``cancel_pending()`` drops in-flight
#   tasks when the daemon stops (an upstream death ends the process).
# - **A duplicate is repaired and reported, never repaired silently.** The worker
#   returns what it found -- the group ids it saw, which one it kept, how many it
#   merged -- and the daemon logs a WARNING naming the agent, the window and the
#   group ids. Repairing quietly would hide what produced the duplicate (two
#   groupings racing, a restored layout, a copy of the group made by hand), and the
#   defect would come back unnoticed.
# - **A closed group is saved, not deleted, and saving is what makes residue.**
#   Chrome keeps a group whose last tab closes as a SAVED group on the bookmarks
#   bar (measured on Chrome 155: the chip reads "Meiva #<id> group - Closed").
#   `chrome.tabGroups.query` returns only open groups, and the extension API has
#   neither a saved-group view nor a delete (`query({saved:true})` is rejected;
#   the namespace exposes only get/move/query/update plus events). The title
#   lookup therefore cannot see a saved group, and the next page for that agent
#   would create a second one — one more closed group per open/close cycle, and
#   saved groups sync into the signed-in user's own Chrome. The daemon ungroups
#   an agent's last tab **before** closing it (`release_page` ->
#   `meivaUngroupLast`): an ungrouped last tab deletes the group instead of
#   saving it (measured). Every way this stack closes an agent's page goes
#   through ``close_page`` (``page_lifecycle``), so that is the one hook needed.
# - **A tab the user closes by hand cannot be cleaned up.** It never passes
#   through ``close_page``, so Chrome saves the group and no API can remove it.
#   Those chips are deleted from the Chrome UI; this stack only guarantees it
#   never adds one itself.
#
# Grouping is presentation only. Every failure -- extension refused, CDP error, no
# unique new target, a wedged Chrome -- is logged as a WARNING and swallowed: the
# browser operation it decorates must succeed or fail on its own merits (see
# docs/decisions/engineering/design/contracts/2026-10-12-presentation-only-failure-degrades-with-warning.md).
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from base.clock import Timers
from base.log import logger
from services.desktop.browser.session import cdp_call, http_json

# meiva:note
# The unpacked extension Chrome loads. Its service-worker file name carries a
# hash of its content: Chrome caches an extension's worker script in the
# profile and keeps running the cached copy when only the content changes, so
# only a new file name reliably ships new code (a test enforces the name).
EXTENSION_DIR = Path(__file__).resolve().parent / "extension"

# meiva:note
# Bound on each of the two target listings. The caller holds the daemon's serial
# lock around them, so a wedged Chrome must not stall every agent's browser call
# for longer than this; a local /json/list answers in milliseconds.
LISTING_TIMEOUT_S = 2.0

# meiva:note
# Bound on one background grouping (load, attach, call), excluding the wait for
# the grouping lock. It holds no daemon lock.
GROUP_TIMEOUT_S = 10.0

# meiva:note
# How long to wait for the extension's service worker target to appear after
# `loadUnpacked` (it normally is up within milliseconds).
WORKER_WAIT_S = 5.0

# meiva:note
# Bound on the pre-close ungroup. It runs inline (the page is about to close, so
# the group must be gone first) and must never delay a close for long: the call
# is one CDP evaluate against an already-loaded worker, and a fresh attach is
# milliseconds. A worker that idled out is not reloaded for this -- it cannot
# have saved a group it never grouped.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
UNGROUP_TIMEOUT_S = 2.0
_WORKER_POLL_S = 0.1


def group_title(agent_id: int) -> str:
    """The tab group title of an agent: ``Meiva #<id>``.

    The title is the group's identity (the worker finds a group by title), so it
    names the agent by id -- labels can repeat, ids cannot. It carries no TTL and
    no color; Chrome picks the color.
    """
    return f"Meiva #{agent_id}"


# meiva:note
# What one convergence found and did, as the worker reports it: the title, the
# window it looked at, the group ids it saw there (before the merge), the group it
# kept, how many groups it merged away and how many tabs it moved. The daemon logs
# a merge as a WARNING, so the numbers have to survive the trip.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
@dataclass(frozen=True)
class Convergence:
    title: str
    window_id: int
    groups: tuple[int, ...]
    kept: int
    merged: int
    moved: int


# meiva:note
# Read one report the worker returned, defensively: the worker is code this module
# does not share a type with, and a malformed report must degrade to a WARNING like
# every other grouping failure rather than escape as a crash.
def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"convergence report field {field!r} must be an integer")
    return value


def _convergence(raw: object) -> Convergence:
    payload: Any = raw
    checked: object = payload
    if not isinstance(checked, dict):
        raise TypeError("convergence report must be an object")
    report: dict[str, Any] = payload
    group_ids_value: Any = report.get("groups")
    ids_check: object = group_ids_value
    if not isinstance(ids_check, list):
        raise TypeError("convergence report must carry the group ids it saw")
    group_ids: list[Any] = group_ids_value
    kept: Any = report.get("kept")
    if kept is None:
        raise TypeError("convergence report must name the group it kept")
    return Convergence(
        title=str(report.get("title", "")),
        window_id=_integer(report.get("windowId"), "windowId"),
        groups=tuple(_integer(i, "groups") for i in group_ids),
        kept=_integer(kept, "kept"),
        merged=_integer(report.get("merged", 0), "merged"),
        moved=_integer(report.get("moved", 0), "moved"),
    )


# meiva:note
# A merged duplicate is a defect that got repaired, not a normal path: name the
# agent, the window and every group id, so the log says what has to stop happening.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
def _warn_merged(report: Convergence, *, who: str) -> None:
    if report.merged <= 0:
        return
    logger.warning(
        "[browser-mcp] {} had {} duplicate tab group(s) titled {!r} in window {} "
        "(groups {}); merged them into group {} so the agent holds one group per window",
        who,
        report.merged,
        report.title,
        report.window_id,
        list(report.groups),
        report.kept,
    )


class _NoUniqueNewPageError(RuntimeError):
    """The page targets did not differ by exactly one between snapshot and grouping."""


async def _page_target_ids(cdp_port: int) -> frozenset[str]:
    raw_listing: Any = await http_json(f"http://127.0.0.1:{cdp_port}/json/list")
    listing: list[dict[str, Any]] = raw_listing if raw_listing is not None else []
    return frozenset(t["id"] for t in listing if t.get("type") == "page")


def _worker_file() -> str:
    """The service-worker file name the shipped manifest declares."""
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
    return manifest["background"]["service_worker"]


# meiva:note
# The service-worker file name this checkout ships (public door to `_worker_file`).
# The session ledger calls the same worker directly, outside the daemon; a second
# reader of the manifest could drift from this one.
# meiva:link services/desktop/browser/docs/browser-session-restore.meiva.okf.md
def shipped_worker() -> str:
    return _worker_file()


async def _live_worker(ws: ClientConnection, ids: Iterator[int], extension_id: str) -> str | None:
    """The target id of this checkout's worker file running in ``extension_id``, if any."""
    url = f"chrome-extension://{extension_id}/{_worker_file()}"
    infos = (await cdp_call(ws, ids, "Target.getTargets", {}))["targetInfos"]
    return next(
        (i["targetId"] for i in infos if i["type"] == "service_worker" and i["url"] == url), None
    )


# meiva:note
# No live worker of the shipped file name, and the caller forbade loading one.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
class _WorkerAbsentError(RuntimeError):
    pass


async def _worker_session(
    ws: ClientConnection,
    ids: Iterator[int],
    *,
    timers: Timers,
    load: bool = True,
    worker_wait_s: float = WORKER_WAIT_S,
) -> str:
    # meiva:note
    # Attach to the extension's service worker, loading the extension if needed.
    #
    # Returns the flattened session id. Reuses a live worker of the shipped file
    # name; otherwise (not loaded, worker idled out, or an older worker file)
    # issues ``Extensions.loadUnpacked`` and waits for the worker to come up.
    # ``load=False`` refuses that instead (``_WorkerAbsentError``): a pre-close ungroup
    # must not start a worker, because a worker that was not running cannot have
    # left a group to save.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    loaded = [
        e
        for e in (await cdp_call(ws, ids, "Extensions.getExtensions", {}))["extensions"]
        if Path(e["path"]) == EXTENSION_DIR
    ]
    target_id = await _live_worker(ws, ids, loaded[0]["id"]) if loaded else None
    if target_id is None:
        if not load:
            raise _WorkerAbsentError("no live tab-group worker to attach to")
        loaded_id = (
            await cdp_call(ws, ids, "Extensions.loadUnpacked", {"path": str(EXTENSION_DIR)})
        )["id"]
        deadline = timers.monotonic() + worker_wait_s
        while (target_id := await _live_worker(ws, ids, loaded_id)) is None:
            if timers.monotonic() >= deadline:
                raise RuntimeError(f"extension {loaded_id} service worker target not found")
            await timers.asleep(_WORKER_POLL_S)
    attached = await cdp_call(
        ws, ids, "Target.attachToTarget", {"targetId": target_id, "flatten": True}
    )
    session_id = attached["sessionId"]
    await _await_worker_script(ws, ids, session_id, timers=timers, worker_wait_s=worker_wait_s)
    return session_id


async def _await_worker_script(
    ws: ClientConnection,
    ids: Iterator[int],
    session_id: str,
    *,
    timers: Timers,
    worker_wait_s: float = WORKER_WAIT_S,
) -> None:
    """Wait until the worker's script has defined ``meivaGroup``.

    The worker's CDP target can be attachable a moment before its script has
    run (measured on first load: ``meivaGroup is not defined``).
    """
    deadline = timers.monotonic() + worker_wait_s
    while True:
        probe = await cdp_call(
            ws,
            ids,
            "Runtime.evaluate",
            {"expression": "typeof meivaGroup", "returnByValue": True},
            session_id=session_id,
        )
        if probe["result"].get("value") == "function":
            return
        if timers.monotonic() >= deadline:
            raise RuntimeError("extension worker did not define meivaGroup")
        await timers.asleep(_WORKER_POLL_S)


# meiva:note
# The grouping facade the daemon holds: pages in, converged groups out.
#
# Built once by the daemon's composition root (with its root ``TaskGroup``, which
# owns the background tasks) and shared by every ``ChromeMcpDaemon`` it creates.
# Callers hold the daemon's serial lock around ``snapshot`` -> ``new_page`` ->
# ``group_new_page``; ``group_new_page`` returns after one short target listing and
# finishes the grouping in a background task. No method raises, and every failure is
# a WARNING.
#
# Every write here converges: the extension re-reads Chrome's open groups by title
# and merges the duplicates of a window instead of trusting a group id it
# remembered, so ``converge_all`` at start and one grouping on each new page repair
# the same defects and are no-ops when there is nothing to repair.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
class AgentTabGroups:
    def __init__(
        self,
        cdp_port: int,
        tasks: asyncio.TaskGroup,
        *,
        timers: Timers,
        listing_timeout_s: float = LISTING_TIMEOUT_S,
        group_timeout_s: float = GROUP_TIMEOUT_S,
        ungroup_timeout_s: float = UNGROUP_TIMEOUT_S,
        worker_wait_s: float = WORKER_WAIT_S,
    ) -> None:
        self._cdp_port = cdp_port
        self._task_group = tasks  # the daemon's root group: it owns every task's lifetime
        self._timers = timers
        self._listing_timeout_s = listing_timeout_s
        self._group_timeout_s = group_timeout_s
        self._ungroup_timeout_s = ungroup_timeout_s
        self._worker_wait_s = worker_wait_s
        self._lock = asyncio.Lock()  # serializes groupings only; the daemon's lock is not held
        self._tasks: set[asyncio.Task[None]] = set()
        # meiva:note
        # page id -> (agent id, CDP target id) for the pages this daemon grouped:
        # the only way to find an agent's tab again at close time, when the
        # upstream hands us a page id and the extension needs a target id.
        # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
        self._pages: dict[int, tuple[int, str]] = {}

    async def snapshot(self) -> frozenset[str] | None:
        """The ids of Chrome's page targets right now, taken before a ``new_page``.

        None when they could not be listed (already reported): the new page then
        stays ungrouped.
        """
        try:
            return await asyncio.wait_for(_page_target_ids(self._cdp_port), self._listing_timeout_s)
        except Exception as e:
            logger.opt(exception=True).warning(
                "[browser-mcp] could not list Chrome page targets for tab grouping "
                "({}: {}); the next new page stays ungrouped",
                type(e).__name__,
                e,
            )
            return None

        # meiva:note
        # Start filing the page created since ``before`` into the agent's tab group.
        #
        # ``before`` is the matching ``snapshot()``; None (its failure was
        # reported) skips. Finds the new page's target with one short listing,
        # then hands the grouping to a background task and returns: the page is
        # grouped moments later, not before this returns. ``page_id`` is the
        # daemon's id for that page; recording it is what lets `release_page`
        # find the tab again when the page closes.
        # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md

    async def group_new_page(
        self, agent_id: int, before: frozenset[str] | None, page_id: int | None = None
    ) -> None:
        if before is None:
            return
        try:
            target_id = await asyncio.wait_for(self._new_target(before), self._listing_timeout_s)
        except Exception as e:
            self._warn(agent_id, e)
            return
        if page_id is not None:
            self._pages[page_id] = (agent_id, target_id)
        task = self._task_group.create_task(
            self._file(agent_id, target_id), name=f"tab-group-agent-{agent_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._settled)

    async def _new_target(self, before: frozenset[str]) -> str:
        new = (await _page_target_ids(self._cdp_port)) - before
        if len(new) != 1:
            raise _NoUniqueNewPageError(
                f"expected exactly one new page target, saw {len(new)} new page targets"
            )
        return next(iter(new))

    # meiva:note
    # The pre-close half: Chrome saves a group when its last tab closes, and a
    # saved group is invisible to the title lookup and undeletable through the
    # extension API, so the group must be gone *before* the tab is. Runs inline
    # under the daemon's lock, bounded by `UNGROUP_TIMEOUT_S`, and reports a
    # failure as a WARNING: a page that must close still closes.
    # Ungroups this page's tab if it is the last member of its agent's group.
    #
    # A no-op for a page this daemon never grouped, or for a group that still
    # has another tab (the extension decides that part, so a user's own group
    # is never touched). Never raises.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    async def release_page(self, page_id: int) -> None:
        recorded = self._pages.pop(page_id, None)
        if recorded is None:
            return
        agent_id, target_id = recorded
        try:
            await asyncio.wait_for(
                self._ungroup_last(group_title(agent_id), target_id), self._ungroup_timeout_s
            )
        except Exception as e:
            logger.opt(exception=not isinstance(e, _WorkerAbsentError)).warning(
                "[browser-mcp] could not ungroup agent {}'s last tab on page {} ({}: {}); "
                "closing it will leave a saved tab group behind",
                agent_id,
                page_id,
                type(e).__name__,
                e,
            )

    async def _ungroup_last(self, title: str, target_id: str) -> None:
        raw_version: Any = await http_json(f"http://127.0.0.1:{self._cdp_port}/json/version")
        version: dict[str, Any] = raw_version if raw_version is not None else {}
        ids = itertools.count(1)
        async with websockets.connect(str(version["webSocketDebuggerUrl"])) as ws:
            session_id = await _worker_session(
                ws, ids, timers=self._timers, load=False, worker_wait_s=self._worker_wait_s
            )
            outcome = await cdp_call(
                ws,
                ids,
                "Runtime.evaluate",
                {
                    "expression": (
                        f"meivaUngroupLast({json.dumps(target_id)}, {json.dumps(title)})"
                    ),
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=session_id,
            )
        if "exceptionDetails" in outcome:
            raise RuntimeError(outcome["exceptionDetails"]["exception"]["description"])

    # meiva:note
    # Converge every agent's groups in every window: the keeper of each (title,
    # window) absorbs its duplicates, and a duplicate it had to merge is a WARNING
    # naming the agent, the window and the group ids.
    #
    # Runs once per served upstream connection (daemon start and every reconnect) and
    # again from the extension itself on Chrome start and extension update, so no
    # restart path has to remember anything. Chrome state is the input, so a run with
    # nothing to repair does nothing and logs nothing.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    async def converge_all(self) -> None:
        try:
            async with self._lock:
                reports = await asyncio.wait_for(self._converge_all_call(), self._group_timeout_s)
        except Exception as e:
            logger.opt(exception=not isinstance(e, TimeoutError)).warning(
                "[browser-mcp] could not converge the agents' tab groups ({}: {}); "
                "every page keeps the group it has",
                type(e).__name__,
                e,
            )
            return
        for report in reports:
            _warn_merged(report, who=f"tab group {report.title!r}")

    def cancel_pending(self) -> None:
        """Drop every in-flight grouping (daemon stop, which an upstream death also causes).

        A dropped grouping leaves its page ungrouped and reports nothing: the
        reason is the daemon's own stop.
        """
        for task in self._tasks:
            task.cancel()

    def _settled(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            task.exception()  # `_file` reports its own failures; this marks it retrieved

    async def _file(self, agent_id: int, target_id: str) -> None:
        title = group_title(agent_id)
        try:
            async with self._lock:
                report = await asyncio.wait_for(
                    self._apply(title, target_id), self._group_timeout_s
                )
        except Exception as e:
            self._warn(agent_id, e)
            return
        _warn_merged(report, who=f"agent {agent_id}")

    @staticmethod
    def _warn(agent_id: int, error: Exception) -> None:
        logger.opt(exception=not isinstance(error, _NoUniqueNewPageError)).warning(
            "[browser-mcp] could not file agent {}'s new page into tab group {!r} "
            "({}: {}); the page is still open and usable",
            agent_id,
            group_title(agent_id),
            type(error).__name__,
            error,
        )

    async def _apply(self, title: str, target_id: str) -> Convergence:
        raw_version: Any = await http_json(f"http://127.0.0.1:{self._cdp_port}/json/version")
        version: dict[str, Any] = raw_version if raw_version is not None else {}
        ids = itertools.count(1)
        # meiva:note
        # Closing the websocket detaches the worker session opened below.
        async with websockets.connect(str(version["webSocketDebuggerUrl"])) as ws:
            session_id = await _worker_session(
                ws, ids, timers=self._timers, worker_wait_s=self._worker_wait_s
            )
            outcome = await cdp_call(
                ws,
                ids,
                "Runtime.evaluate",
                {
                    "expression": f"meivaGroup({json.dumps(target_id)}, {json.dumps(title)})",
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=session_id,
            )
        if "exceptionDetails" in outcome:
            raise RuntimeError(outcome["exceptionDetails"]["exception"]["description"])
        return _convergence(outcome["result"].get("value"))

    async def _converge_all_call(self) -> list[Convergence]:
        raw_version: Any = await http_json(f"http://127.0.0.1:{self._cdp_port}/json/version")
        version: dict[str, Any] = raw_version if raw_version is not None else {}
        ids = itertools.count(1)
        async with websockets.connect(str(version["webSocketDebuggerUrl"])) as ws:
            session_id = await _worker_session(
                ws, ids, timers=self._timers, worker_wait_s=self._worker_wait_s
            )
            outcome = await cdp_call(
                ws,
                ids,
                "Runtime.evaluate",
                {
                    "expression": "meivaConvergeAll()",
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=session_id,
            )
        if "exceptionDetails" in outcome:
            raise RuntimeError(outcome["exceptionDetails"]["exception"]["description"])
        raw: Any = outcome["result"].get("value")
        listed: object = raw
        if not isinstance(listed, list):
            raise TypeError("meivaConvergeAll did not return a list of reports")
        return [_convergence(item) for item in raw]
