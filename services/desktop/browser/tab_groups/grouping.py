# meiva:note
# One Chrome tab group per agent, for the pages that agent's browser calls create.
#
# Chrome's DevTools protocol has no tab-group API (measured on Chrome 153: none
# of its domains mentions groups), so grouping goes through a small extension
# shipped in ``extension/`` and staged into ``<home>/run/tab-groups-extension``.
# The browser-mcp daemon loads that single staged directory with
# ``Extensions.loadUnpacked`` -- available on the ordinary
# ``--remote-debugging-port`` connection, no launch flag -- uninstalls any older
# unpacked ``Meiva Tab Groups`` copies left by earlier checkouts, and calls the
# ``meivaGroup(targetId, title)`` function its service worker exposes.
#
# The invariant is stated once, here: an agent's pages sit in at most one open
# tab group across the entire browser, titled ``Meiva #<id>``. Every entry point
# CONVERGES rather than assumes: it re-reads Chrome's open groups by title across
# all windows, keeps one deterministic group (most tabs, ties to lowest group id),
# and moves any duplicate groups' tabs into it while keeping the keeper anchored
# in its window. Convergence is a function of Chrome's state alone -- nothing is
# remembered -- so a daemon restart, a Chrome restart, an extension update and a
# repeated run all land in the same place, and the second run of any of them
# changes nothing.
#
# The facts that shape the design:
#
# - **One staged directory and a fixed manifest key.** Loading directly from a
#   versioned source checkout left one ``Meiva Tab Groups`` copy per release in a
#   long-lived Chrome, each with a fresh path-derived extension id that lost any
#   toolbar unpin decision. Staging into ``<home>/run/tab-groups-extension`` with
#   a fixed manifest ``key`` gives the extension one permanent id, and every
#   worker check uninstalls any ``Meiva Tab Groups`` copy loaded from another path.
# - **Loading is per Chrome session and the worker idles out.** An unpacked
#   extension loaded over CDP is gone after a Chrome restart, and its service
#   worker stops ~30s after its last event (its CDP target disappears, or stays
#   listed on Chrome 156 while refusing ``Target.attachToTarget``). Each worker
#   call therefore checks whether the staged extension has an attachable worker of
#   the file name this checkout ships, and issues ``Extensions.loadUnpacked`` only
#   when it does not (Chrome restarted, worker idled out, or an older worker file
#   is still running). It never reloads a live attachable worker: reloading tears
#   the worker down under in-flight calls (measured). After attaching it waits for
#   the worker script to have defined ``meivaGroup``.
# - **One lock across every extension caller.** ``ExtensionWorker`` serializes
#   ``session_restore.reapply``, ``converge_all``, background ``meivaGroup`` tasks
#   and pre-close ``release_page`` calls so two callers never reload the extension
#   underneath each other or race a group creation against an immediate close.
# - **Page ids are not target ids.** The daemon's page ids are chrome-devtools-mcp
#   sequence numbers; the extension needs the CDP targetId. The caller holds the
#   daemon's machine-wide serial lock, so the one page target that appears
#   between ``snapshot()`` (before ``new_page``) and ``group_new_page()`` (after)
#   is the page that was just created. Anything other than exactly one new
#   target (a user opening a tab meanwhile, a popup) is reported and left
#   ungrouped -- the module never guesses which tab is the agent's.
# - **Only the two target listings run under the daemon lock on creation.** They
#   are short (``LISTING_TIMEOUT_S``). Loading the extension, attaching to its
#   worker and calling ``meivaGroup`` run in a background task with its own
#   timeout (``GROUP_TIMEOUT_S``), so a slow or wedged Chrome costs page creation
#   nothing. ``cancel_pending()`` drops in-flight tasks when the daemon stops.
# - **A duplicate is repaired and reported, never repaired silently.** The worker
#   returns what it found -- the group ids it saw, which one it kept, how many it
#   merged -- and the daemon logs a WARNING naming the agent, the window and the
#   group ids.
# - **A closed group is saved, not deleted, and saving is what makes residue.**
#   Chrome keeps a group whose last tab closes as a SAVED group on the bookmarks
#   bar (measured on Chrome 155: the chip reads "Meiva #<id> group - Closed").
#   ``chrome.tabGroups.query`` returns only open groups, and the extension API has
#   neither a saved-group view nor a delete. The daemon therefore ungroups an
#   agent's last tab **before** closing it (``release_page`` ->
#   ``meivaUngroupLast``), waking an idled-out worker when needed: an ungrouped
#   last tab deletes the group instead of saving it (measured).
# - **A tab the user closes by hand cannot be cleaned up.** It never passes
#   through ``close_page``, so Chrome saves the group and no API can remove it.
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
from base.host.atomic_io import write_bytes_atomic
from base.log import logger
from base.paths import run_dir
from services.desktop.browser.session import cdp_call, http_json

# meiva:note
# The unpacked extension files shipped inside the package. The service-worker
# file name carries a hash of its content: Chrome caches an extension's worker
# script in the profile and keeps running the cached copy when only the content
# changes, so only a new file name reliably ships new code (a test enforces it).
EXTENSION_DIR = Path(__file__).resolve().parent / "extension"
EXTENSION_NAME = "Meiva Tab Groups"
STAGED_EXTENSION_DIR_NAME = "tab-groups-extension"

# meiva:note
# Bound on each of the two target listings. The caller holds the daemon's serial
# lock around them, so a wedged Chrome must not stall every agent's browser call
# for longer than this; a local /json/list answers in milliseconds.
LISTING_TIMEOUT_S = 2.0

# meiva:note
# Bound on one background grouping or convergence pass.
GROUP_TIMEOUT_S = 10.0

# meiva:note
# How long to wait for the extension's service worker target to appear after
# `loadUnpacked` (it normally is up within milliseconds).
WORKER_WAIT_S = 5.0

# meiva:note
# Bound on the pre-close ungroup. It runs inline before `close_page` and wakes
# the service worker via `loadUnpacked` when Chrome stopped the idle worker
# (~30s after the last grouping event), so its budget matches `WORKER_WAIT_S`.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
UNGROUP_TIMEOUT_S = 5.0
_WORKER_POLL_S = 0.1


# meiva:note
# The tab group title of an agent: `Meiva #<id>`. The title is the group's
# identity (the worker finds a group by title), so it names the agent by id --
# labels can repeat, ids cannot. Chrome assigns the color.
def group_title(agent_id: int) -> str:
    return f"Meiva #{agent_id}"


# meiva:note
# Single per-home runtime directory Chrome loads `Meiva Tab Groups` from
# (`<home>/run/tab-groups-extension`), so release checkouts and worktrees never
# accumulate duplicate unpacked extension entries in a long-lived browser.
def staged_extension_dir(home: Path) -> Path:
    return run_dir(home) / STAGED_EXTENSION_DIR_NAME


# meiva:note
# Sync the shipped `manifest.json` and hashed worker script into `extension_dir`
# atomically (worker first, manifest second, then unlink older `group-*.js`
# files). A no-op when `extension_dir` is `EXTENSION_DIR` itself.
def stage_extension(extension_dir: Path) -> None:
    if extension_dir.resolve() == EXTENSION_DIR.resolve():
        return
    extension_dir.mkdir(parents=True, exist_ok=True)
    worker_name = _worker_file()
    write_bytes_atomic(
        extension_dir / worker_name,
        (EXTENSION_DIR / worker_name).read_bytes(),
        mode=0o644,
    )
    write_bytes_atomic(
        extension_dir / "manifest.json",
        (EXTENSION_DIR / "manifest.json").read_bytes(),
        mode=0o644,
    )
    for stale in extension_dir.glob("group-*.js"):
        if stale.name != worker_name:
            stale.unlink(missing_ok=True)


# meiva:note
# What one convergence found and did, as the worker reports it: the title, the
# keeper's window, the group ids it saw across the browser (before the merge),
# the group it kept, how many groups it merged away and how many tabs it moved.
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
# agent, the keeper window and every group id, so the log says what happened.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
def _warn_merged(report: Convergence, *, who: str) -> None:
    if report.merged <= 0:
        return
    logger.warning(
        "[browser-mcp] {} had {} duplicate tab group(s) titled {!r} "
        "(groups {}); merged them into group {} in window {} so the agent holds one group",
        who,
        report.merged,
        report.title,
        list(report.groups),
        report.kept,
        report.window_id,
    )


# meiva:note
# The page targets did not differ by exactly one between snapshot and grouping.
class _NoUniqueNewPageError(RuntimeError):
    pass


async def _page_target_ids(cdp_port: int) -> frozenset[str]:
    raw_listing: Any = await http_json(f"http://127.0.0.1:{cdp_port}/json/list")
    listing: list[dict[str, Any]] = raw_listing if raw_listing is not None else []
    return frozenset(t["id"] for t in listing if t.get("type") == "page")


# meiva:note
# The service-worker file name the shipped manifest declares.
def _worker_file() -> str:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest["background"]["service_worker"])


# meiva:note
# The service-worker file name this checkout ships (public door to `_worker_file`).
# meiva:link services/desktop/browser/docs/browser-session-restore.meiva.okf.md
def shipped_worker() -> str:
    return _worker_file()


async def _worker_targets(ws: ClientConnection, ids: Iterator[int], extension_id: str) -> list[str]:
    url = f"chrome-extension://{extension_id}/{_worker_file()}"
    infos = (await cdp_call(ws, ids, "Target.getTargets", {}))["targetInfos"]
    return [
        str(info["targetId"])
        for info in infos
        if info.get("type") == "service_worker" and info.get("url") == url
    ]


# meiva:note
# Remove any unpacked `Meiva Tab Groups` copies loaded from older checkout or
# worktree paths so Chrome keeps only `extension_dir`. Never touches extensions
# with any other name.
async def _uninstall_stale_extensions(
    ws: ClientConnection,
    ids: Iterator[int],
    extensions: list[dict[str, Any]],
    *,
    extension_dir: Path,
) -> None:
    target = extension_dir.resolve()
    for entry in extensions:
        entry_path = entry.get("path")
        entry_id = entry.get("id")
        if (
            entry.get("name") != EXTENSION_NAME
            or not isinstance(entry_path, str)
            or not isinstance(entry_id, str)
            or Path(entry_path).resolve() == target
        ):
            continue
        try:
            await cdp_call(ws, ids, "Extensions.uninstall", {"id": entry_id})
        except Exception as exc:
            logger.opt(exception=True).warning(
                "[browser-mcp] could not uninstall stale {!r} extension {} at {} ({}: {})",
                EXTENSION_NAME,
                entry_id,
                entry_path,
                type(exc).__name__,
                exc,
            )


# meiva:note
# Attach to the extension's service worker, staging and loading `extension_dir`
# when no attachable worker of the shipped file name is running.
#
# Chrome 156 can keep an idled-out worker listed in `Target.getTargets` while
# rejecting `Target.attachToTarget`; trying the newest listed target first and
# falling through to `Extensions.loadUnpacked` on `RuntimeError` wakes the worker
# without reloading a live one.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
async def _worker_session(
    ws: ClientConnection,
    ids: Iterator[int],
    *,
    timers: Timers,
    extension_dir: Path = EXTENSION_DIR,
    worker_wait_s: float = WORKER_WAIT_S,
) -> str:
    extensions: list[dict[str, Any]] = (await cdp_call(ws, ids, "Extensions.getExtensions", {}))[
        "extensions"
    ]
    await _uninstall_stale_extensions(ws, ids, extensions, extension_dir=extension_dir)
    target = extension_dir.resolve()
    loaded = [
        e
        for e in extensions
        if isinstance(e.get("path"), str) and Path(e["path"]).resolve() == target
    ]
    session_id: str | None = None
    if loaded:
        for target_id in reversed(await _worker_targets(ws, ids, str(loaded[0]["id"]))):
            try:
                attached = await cdp_call(
                    ws, ids, "Target.attachToTarget", {"targetId": target_id, "flatten": True}
                )
                session_id = str(attached["sessionId"])
                break
            except RuntimeError:
                continue
    if session_id is None:
        stage_extension(extension_dir)
        loaded_id = str(
            (await cdp_call(ws, ids, "Extensions.loadUnpacked", {"path": str(extension_dir)}))["id"]
        )
        deadline = timers.monotonic() + worker_wait_s
        while not (targets := await _worker_targets(ws, ids, loaded_id)):
            if timers.monotonic() >= deadline:
                raise RuntimeError(f"extension {loaded_id} service worker target not found")
            await timers.asleep(_WORKER_POLL_S)
        attached = await cdp_call(
            ws, ids, "Target.attachToTarget", {"targetId": targets[-1], "flatten": True}
        )
        session_id = str(attached["sessionId"])
    await _await_worker_script(ws, ids, session_id, timers=timers, worker_wait_s=worker_wait_s)
    return session_id


# meiva:note
# Wait until the worker's script has defined `meivaGroup` (its CDP target can be
# attachable a moment before top-level script evaluation finishes).
async def _await_worker_script(
    ws: ClientConnection,
    ids: Iterator[int],
    session_id: str,
    *,
    timers: Timers,
    worker_wait_s: float = WORKER_WAIT_S,
) -> None:
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
# Single serialized runner for evaluating expressions in the tab-group service
# worker over CDP. Shared by `AgentTabGroups` and `session_restore.CdpChrome` in
# `browser-mcp` so startup layout restore, convergence, page grouping and
# pre-close ungrouping never reload the extension underneath each other.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
class ExtensionWorker:
    def __init__(
        self,
        cdp_port: int,
        *,
        timers: Timers,
        extension_dir: Path = EXTENSION_DIR,
        worker_wait_s: float = WORKER_WAIT_S,
    ) -> None:
        self._cdp_port = cdp_port
        self._timers = timers
        self._extension_dir = extension_dir
        self._worker_wait_s = worker_wait_s
        self._lock = asyncio.Lock()

    async def evaluate(self, expression: str, *, timeout_s: float) -> object:
        async with self._lock:
            return await asyncio.wait_for(self._evaluate_unlocked(expression), timeout_s)

    async def _evaluate_unlocked(self, expression: str) -> object:
        raw_version: Any = await http_json(f"http://127.0.0.1:{self._cdp_port}/json/version")
        checked: object = raw_version
        if not isinstance(checked, dict):
            raise TypeError("CDP /json/version did not return an object")
        version: dict[str, Any] = raw_version
        ids = itertools.count(1)
        # meiva:note
        # Closing the websocket detaches the worker session opened below.
        async with websockets.connect(str(version["webSocketDebuggerUrl"])) as ws:
            session_id = await _worker_session(
                ws,
                ids,
                timers=self._timers,
                extension_dir=self._extension_dir,
                worker_wait_s=self._worker_wait_s,
            )
            outcome = await cdp_call(
                ws,
                ids,
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=session_id,
            )
        if "exceptionDetails" in outcome:
            raise RuntimeError(outcome["exceptionDetails"]["exception"]["description"])
        return outcome["result"].get("value")


# meiva:note
# The grouping facade the daemon holds: pages in, converged groups out.
#
# Built once by the daemon's composition root (with its root `TaskGroup`, which
# owns the background tasks, and its shared `ExtensionWorker`) and shared by
# `ChromeMcpDaemon`. Callers hold the daemon's serial lock around `snapshot` ->
# `new_page` -> `group_new_page`; `group_new_page` returns after one short target
# listing and finishes the grouping in a background task. No method raises, and
# every failure is a WARNING.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
class AgentTabGroups:
    def __init__(
        self,
        cdp_port: int,
        tasks: asyncio.TaskGroup,
        *,
        worker: ExtensionWorker,
        listing_timeout_s: float = LISTING_TIMEOUT_S,
        group_timeout_s: float = GROUP_TIMEOUT_S,
        ungroup_timeout_s: float = UNGROUP_TIMEOUT_S,
    ) -> None:
        self._cdp_port = cdp_port
        self._task_group = tasks
        self._worker = worker
        self._listing_timeout_s = listing_timeout_s
        self._group_timeout_s = group_timeout_s
        self._ungroup_timeout_s = ungroup_timeout_s
        self._tasks: set[asyncio.Task[None]] = set()
        # meiva:note
        # page id -> (agent id, CDP target id) for the pages this daemon grouped:
        # the only way to find an agent's tab again at close time, when the
        # upstream hands us a page id and the extension needs a target id.
        # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
        self._pages: dict[int, tuple[int, str]] = {}

    # meiva:note
    # The ids of Chrome's page targets right now, taken before a `new_page`.
    # None when they could not be listed (already reported): the new page then
    # stays ungrouped.
    async def snapshot(self) -> frozenset[str] | None:
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
    # Start filing the page created since `before` into the agent's tab group.
    # `before` is the matching `snapshot()`; None skips. Finds the new page's
    # target with one short listing, records `page_id` for `release_page`, then
    # hands the grouping to a background task and returns.
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
    # under the daemon's lock, bounded by `UNGROUP_TIMEOUT_S` (including waiting
    # on `ExtensionWorker`'s lock and waking an idled-out worker), and reports a
    # failure as a WARNING: a page that must close still closes.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    async def release_page(self, page_id: int) -> None:
        recorded = self._pages.pop(page_id, None)
        if recorded is None:
            return
        agent_id, target_id = recorded
        try:
            await asyncio.wait_for(
                self._worker.evaluate(
                    f"meivaUngroupLast({json.dumps(target_id)}, {json.dumps(group_title(agent_id))})",
                    timeout_s=self._ungroup_timeout_s,
                ),
                self._ungroup_timeout_s,
            )
        except Exception as e:
            logger.opt(exception=not isinstance(e, TimeoutError)).warning(
                "[browser-mcp] could not ungroup agent {}'s last tab on page {} ({}: {}); "
                "closing it will leave a saved tab group behind",
                agent_id,
                page_id,
                type(e).__name__,
                e,
            )

    # meiva:note
    # Converge every agent's groups across the browser: the keeper of each title
    # absorbs its duplicates, and any duplicate merged is logged as a WARNING.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    async def converge_all(self) -> None:
        try:
            reports = await self._converge_all_call()
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

    # meiva:note
    # Drop every in-flight grouping when the daemon stops; leaves its page
    # ungrouped and reports nothing because the daemon itself is stopping.
    def cancel_pending(self) -> None:
        for task in self._tasks:
            task.cancel()

    def _settled(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            # meiva:note
            # `_file` reports its own failures; reading `exception()` marks it retrieved.
            task.exception()

    async def _file(self, agent_id: int, target_id: str) -> None:
        title = group_title(agent_id)
        try:
            raw = await self._worker.evaluate(
                f"meivaGroup({json.dumps(target_id)}, {json.dumps(title)})",
                timeout_s=self._group_timeout_s,
            )
            report = _convergence(raw)
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

    async def _converge_all_call(self) -> list[Convergence]:
        raw: Any = await self._worker.evaluate(
            "meivaConvergeAll()", timeout_s=self._group_timeout_s
        )
        listed: object = raw
        if not isinstance(listed, list):
            raise TypeError("meivaConvergeAll did not return a list of reports")
        return [_convergence(item) for item in raw]
