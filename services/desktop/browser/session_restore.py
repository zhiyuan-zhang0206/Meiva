# meiva:note
# The shared browser's tab set across a deliberate stop -> start.
#
# Chrome keeps its own session in `$MEIVA_HOME/chrome-profile/`, so relaunching
# the profile with `--restore-last-session` reopens what it last held. Measured
# on Chrome 156 (macOS, throwaway profile, the SIGTERM a `meiva stop` sends):
#
#   * SIGTERM is a clean exit — `profile.exit_type` becomes `SessionEnded` and
#     `Default/Sessions/Session_*` is written — and the flag then restored the
#     URL set and its order 9/9 times, with pinned state and tab-group titles
#     8/9. The "continue where you left off" preference is NOT a substitute: on
#     its own it restored 2 of 4 tabs and dropped both grouped ones.
#   * `kill -9` leaves Preferences unwritten (or an older `exit_type` behind), so
#     Chrome's own exit marker can never be the crash gate. This module's
#     `pending` field is, and only `meiva stop --stop-browser` writes it.
#
# The ledger is therefore a record plus the one repair the flag cannot promise:
# the pinned/group half is re-applied from it by `reapply` (one extension call),
# which makes the 8/9 deterministic and, when it fails, degrades to "every URL is
# back, the pin is not" instead of a lost tab.
#
# `Chrome` is the seam: the module never reaches for CDP itself, so a test drives
# it with a fake and `CdpChrome` is the only adapter that talks to a real one.
# meiva:link services/desktop/browser/docs/browser-session-restore.meiva.okf.md
from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import websockets

from base.host.atomic_io import write_text_atomic
from base.log import logger
from base.paths import run_dir

from .session import cdp_call, http_json
from .tab_groups.grouping import EXTENSION_DIR, shipped_worker

LEDGER_NAME = "browser-session.json"

# meiva:note
# A local `/json/list` answers in milliseconds; the bound only covers a wedged
# Chrome, and the stop phase that calls this must never hold the shutdown open.
LISTING_TIMEOUT_S = 2.0

# meiva:note
# One extension call, plus the retries the service worker needs: a worker that
# idled out stays listed as a target but refuses attach, so a failed attempt is
# retried after a reload (see `CdpChrome.worker`). The whole ladder is bounded by
# WORKER_ATTEMPTS * WORKER_TIMEOUT_S, and every caller treats a failure as
# "degrade", never as a failed stop or a failed start.
WORKER_TIMEOUT_S = 4.0
WORKER_ATTEMPTS = 3
WORKER_RETRY_S = 0.4
WORKER_SCRIPT_WAIT_S = 5.0

# meiva:note
# How long `meiva start` waits for the daemon's reapply before it reports. The
# reapply runs as a background task in browser-mcp, so a health-gated readiness
# can finish first; the wait turns a coin flip into "almost always reported".
# (Named REPORT_WAIT_S, not REAPPLY_*_S: the clock-lattice lint reads the
# substring "REAP" in "REAPPLY" as reap-window vocabulary, and this constant is
# a report wait with no lattice neighbour.)
REPORT_WAIT_S = 10.0
REPORT_POLL_S = 0.25

PENDING_RESTORE = "restore"
PENDING_LAYOUT = "layout"

Fetch = Callable[[str], Awaitable[Any]]
Connect = Callable[[str], AbstractAsyncContextManager[Any]]


# meiva:note
# The two reads this module needs from a browser.
class Chrome(Protocol):
    # meiva:note
    # GET one of CDP's HTTP endpoints (`/json/list`, `/json/version`).
    async def json(self, path: str) -> Any: ...

    # meiva:note
    # Evaluate `expression` in the tab-group extension's worker and return its value.
    async def worker(self, expression: str) -> Any: ...


# meiva:note
# One recorded tab. No cookie, token or header is ever part of this record.
@dataclass(frozen=True)
class Tab:
    url: str
    title: str
    pinned: bool
    group: str | None
    index: int
    window: int


@dataclass(frozen=True)
class Ledger:
    tabs: tuple[Tab, ...]
    captured_at: str
    layout: bool
    pending: str | None
    reapply: dict[str, int] | None
    report_pending: bool = False

    # meiva:note
    # The recorded URLs, ignoring the browser's own `about:blank` tab.
    @property
    def urls(self) -> list[str]:
        return [tab.url for tab in self.tabs if tab.url and tab.url != "about:blank"]

    # meiva:note
    # How many layout attributes the reapply is owed: pins plus grouped tabs.
    @property
    def layout_expected(self) -> int:
        return sum(1 for tab in self.tabs if tab.pinned) + sum(
            1 for tab in self.tabs if tab.group is not None
        )


# meiva:note
# This unit's browser session ledger (`<home>/run/browser-session.json`).
#
#     Beside the other lifecycle state (`start-serving.json`, `pty-sessions.json`):
#     it must survive a stop and is read by different processes.
def ledger_path(home: Path) -> Path:
    return run_dir(home) / LEDGER_NAME


# meiva:note
# The ledger, or None when this unit never recorded one (or it is unreadable).
def read(home: Path) -> Ledger | None:
    path = ledger_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("[browser-session] ignoring unreadable {}: {}", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("[browser-session] ignoring malformed ledger {}", path)
        return None
    stored = cast("dict[str, Any]", raw)
    try:
        tabs = tuple(
            Tab(
                url=str(item["url"]),
                title=str(item.get("title", "")),
                pinned=bool(item.get("pinned", False)),
                group=item.get("group"),
                index=int(item.get("index", 0)),
                window=int(item.get("window", 0)),
            )
            for item in cast("list[dict[str, Any]]", stored["tabs"])
        )
        return Ledger(
            tabs=tabs,
            captured_at=str(stored.get("captured_at", "")),
            layout=bool(stored.get("layout", False)),
            pending=cast("str | None", stored.get("pending")),
            reapply=cast("dict[str, int] | None", stored.get("reapply")),
            report_pending=bool(stored.get("report_pending", False)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("[browser-session] ignoring malformed ledger {}: {}", path, exc)
        return None


# meiva:note
# Publish the ledger atomically; the home is private, the file is 0600.
def write(home: Path, ledger: Ledger) -> None:
    path = ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": ledger.captured_at,
        "layout": ledger.layout,
        "pending": ledger.pending,
        "reapply": ledger.reapply,
        "report_pending": ledger.report_pending,
        "tabs": [
            {
                "url": tab.url,
                "title": tab.title,
                "pinned": tab.pinned,
                "group": tab.group,
                "index": tab.index,
                "window": tab.window,
            }
            for tab in ledger.tabs
        ],
    }
    write_text_atomic(path, json.dumps(payload, indent=2) + "\n", mode=0o600)


# meiva:note
# Consume a pending restore: True means launch Chrome with `--restore-last-session`.
#
#     Consumed before Chrome starts on purpose: a second start must not replay the
#     same session again. The ledger moves to the reapply stage, which is what the
#     browser-mcp daemon finishes once Chrome answers.
def begin_launch(home: Path) -> bool:
    ledger = read(home)
    if ledger is None or ledger.pending != PENDING_RESTORE:
        return False
    write(home, replace(ledger, pending=PENDING_LAYOUT))
    return True


# meiva:note
# ── capture: what is open now ──────────────────────────────────────────────


# meiva:note
# Record the browser's open tabs, or None when there is no browser to record.
async def capture(chrome: Chrome) -> Ledger | None:
    windows = await _extension_layout(chrome)
    if windows is None:
        urls = await _listed_urls(chrome)
        if urls is None:
            return None
        return Ledger(
            tabs=tuple(
                Tab(url=url, title="", pinned=False, group=None, index=index, window=0)
                for index, url in enumerate(urls)
            ),
            captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
            layout=False,
            pending=PENDING_RESTORE,
            reapply=None,
            report_pending=True,
        )
    tabs: list[Tab] = []
    for window_index, window in enumerate(windows):
        for entry in window:
            tabs.append(
                Tab(
                    url=str(entry.get("url", "")),
                    title=str(entry.get("title", "")),
                    pinned=bool(entry.get("pinned", False)),
                    group=entry.get("group") if isinstance(entry.get("group"), str) else None,
                    index=int(entry.get("index", 0)),
                    window=window_index,
                )
            )
    if not any(tab.url for tab in tabs):
        return None
    return Ledger(
        tabs=tuple(tabs),
        captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
        layout=True,
        pending=PENDING_RESTORE,
        reapply=None,
        report_pending=True,
    )


# meiva:note
# Every window's tabs as the extension sees them, or None when it cannot be asked.
async def _extension_layout(chrome: Chrome) -> list[list[dict[str, Any]]] | None:
    try:
        raw = await chrome.worker("meivaLayout()")
    except Exception as exc:
        logger.opt(exception=not isinstance(exc, TimeoutError)).warning(
            "[browser-session] the tab-group worker could not report the layout ({}: {}); "
            "the ledger records URLs only",
            type(exc).__name__,
            exc,
        )
        return None
    if not isinstance(raw, list):
        return None
    windows: list[list[dict[str, Any]]] = []
    for window in cast("list[Any]", raw):
        if not isinstance(window, list):
            continue
        rows = cast("list[Any]", window)
        windows.append([cast("dict[str, Any]", row) for row in rows if isinstance(row, dict)])
    return windows


# meiva:note
# `/json/list` URLs of the page targets, or None when Chrome does not answer.
async def _listed_urls(chrome: Chrome) -> list[str] | None:
    try:
        listing = await chrome.json("/json/list")
    except Exception as exc:
        logger.opt(exception=not isinstance(exc, TimeoutError)).warning(
            "[browser-session] could not list Chrome's pages ({}: {})", type(exc).__name__, exc
        )
        return None
    if not isinstance(listing, list):
        return None
    return [
        str(target.get("url", ""))
        for target in cast("list[dict[str, Any]]", listing)
        if target.get("type") == "page"
    ]


# meiva:note
# ── re-applying the layout the flag can drop ───────────────────────────────


# meiva:note
# The extension's `meivaApplyLayout` argument: pins first, then groups by title.
def layout_spec(ledger: Ledger) -> dict[str, Any]:
    pins = [tab.url for tab in ledger.tabs if tab.pinned and tab.url]
    grouped: dict[str, list[str]] = {}
    for tab in ledger.tabs:
        if tab.group is not None and tab.url:
            grouped.setdefault(str(tab.group), []).append(tab.url)
    return {
        "pins": pins,
        "groups": [{"title": title, "urls": urls} for title, urls in grouped.items()],
    }


# meiva:note
# Finish a restore: put the recorded pins and groups back, then record how it went.
#
#     Runs in the browser-mcp daemon once Chrome answers. Every failure is a
#     warning: the tabs Chrome restored stay exactly as they are.
async def reapply(home: Path, chrome: Chrome) -> None:
    ledger = read(home)
    if ledger is None or ledger.pending != PENDING_LAYOUT:
        return
    spec = layout_spec(ledger)
    expected = ledger.layout_expected
    try:
        outcome = await chrome.worker(f"meivaApplyLayout({json.dumps(spec)})")
    except Exception as exc:
        logger.opt(exception=not isinstance(exc, TimeoutError)).warning(
            "[browser-session] could not re-apply the recorded pins/groups ({}: {}); "
            "the restored tabs are untouched",
            type(exc).__name__,
            exc,
        )
        write(home, replace(ledger, pending=None, reapply={"applied": 0, "expected": expected}))
        return
    counts = cast("dict[str, Any]", outcome) if isinstance(outcome, dict) else {}
    applied = int(counts.get("pinned", 0)) + int(counts.get("grouped", 0))
    logger.info("[browser-session] re-applied {}/{} recorded pins/groups", applied, expected)
    write(home, replace(ledger, pending=None, reapply={"applied": applied, "expected": expected}))


# meiva:note
# ── reporting what actually happened ───────────────────────────────────────


# meiva:note
# How many recorded URLs are open now / how many were recorded.
#
#     Matching is by multiset, so two tabs with the same URL count twice and the
#     browser's own `about:blank` tab (which a launch adds) is never a mismatch.
async def _restored_counts(chrome: Chrome, recorded: list[str]) -> tuple[int, int]:
    live = await _listed_urls(chrome)
    if live is None:
        return 0, len(recorded)
    remaining = list(recorded)
    restored = 0
    for url in live:
        if url in remaining:
            remaining.remove(url)
            restored += 1
    return restored, len(recorded)


# meiva:note
# The `meiva start` self-proof line, or None when this start owes no restore.
#
#     Printed as two counts on purpose: URL restoration and the pin/group reapply
#     fail independently, and one number would hide the second (`8/9` loss shape).
def report(*, home: Path, chrome: Chrome, wait_s: float | None = None) -> str | None:
    ledger = read(home)
    if ledger is None or not ledger.report_pending:
        return None
    # meiva:note
    # The report is keyed on `report_pending`, not on the handshake field: the
    # daemon's reapply can finish before this line runs, and keying it on
    # `pending` made a completed restore print nothing.
    # meiva:note
    # `restore` still pending means Chrome was never relaunched; leave the record
    # for the start that does launch the browser.
    if ledger.pending == PENDING_RESTORE:
        return None
    deadline = time.monotonic() + (REPORT_WAIT_S if wait_s is None else wait_s)
    while ledger.pending == PENDING_LAYOUT and time.monotonic() < deadline:
        time.sleep(REPORT_POLL_S)
        ledger = read(home) or ledger
    restored, expected = asyncio.run(_restored_counts(chrome, ledger.urls))
    line = f"browser session: restored {restored}/{expected} tabs"
    if not ledger.layout:
        line += "; pin/group not recorded (the worker was unavailable at stop)"
    elif ledger.reapply is None:
        line += "; pin/group reapply still running"
    else:
        line += f"; pin/group reapplied {ledger.reapply['applied']}/{ledger.reapply['expected']}"
    write(home, replace(ledger, pending=None, report_pending=False))
    return line


# meiva:note
# Write the ledger for a stop that is about to stop the browser; never raises.
#
#     Returns the line the stop prints, or None when there was nothing to report.
#     Called before the services phase: by the time the stop's own `browser` phase
#     runs, the `meiva-browser` session has already been signalled and Chrome is on
#     its way out. A stop that cannot record still stops.
def record_for_stop(*, home: Path, chrome: Chrome) -> str | None:
    try:
        ledger = asyncio.run(capture(chrome))
    except Exception as exc:
        logger.opt(exception=True).warning(
            "[browser-session] could not record the browser's tabs ({}: {}); "
            "the next start restores nothing",
            type(exc).__name__,
            exc,
        )
        return None
    if ledger is None:
        logger.info("[browser-session] no browser to record; nothing will be restored")
        return "browser session: nothing to record (no browser answered)"
    try:
        write(home, ledger)
    except OSError as exc:
        logger.opt(exception=True).warning(
            "[browser-session] could not write {} ({}); the next start restores nothing",
            ledger_path(home),
            exc,
        )
        return None
    pins = sum(1 for tab in ledger.tabs if tab.pinned)
    groups = len({tab.group for tab in ledger.tabs if tab.group})
    shape = f"{pins} pinned, {groups} tab group(s)" if ledger.layout else "URLs only"
    return f"browser session: recorded {len(ledger.urls)} tab(s) ({shape})"


# meiva:note
# ── the CDP adapter ────────────────────────────────────────────────────────


def _connect(url: str) -> AbstractAsyncContextManager[Any]:
    return cast("AbstractAsyncContextManager[Any]", websockets.connect(url))


# meiva:note
# `Chrome` over this unit's CDP port.
#
#     `fetch` and `connect` are injectable so the worker ladder below can be driven
#     by a fake; production passes neither and gets the real HTTP client and
#     websocket.
class CdpChrome:
    def __init__(
        self, cdp_port: int, *, fetch: Fetch | None = None, connect: Connect | None = None
    ) -> None:
        self._cdp_port = cdp_port
        self._fetch = fetch or http_json
        self._connect = connect or _connect

    async def json(self, path: str) -> Any:
        url = f"http://127.0.0.1:{self._cdp_port}{path}"
        return await asyncio.wait_for(self._fetch(url), LISTING_TIMEOUT_S)

    # meiva:note
    # Evaluate `expression` in the tab-group extension's worker.
    #
    #         The worker is the only thing that can see (and set) `pinned` and tab
    #         groups; CDP has neither. Attaching is the fragile part: a worker that has
    #         idled out stays listed as a target but rejects `Target.attachToTarget`,
    #         and reloading a live one tears it down mid-call. So an attempt reuses a
    #         live worker, and a failed attempt reloads the extension before the next
    #         one (measured on Chrome 156: the first call after a load always answers;
    #         later calls need this ladder).
    async def worker(self, expression: str) -> Any:
        last: BaseException | None = None
        for attempt in range(WORKER_ATTEMPTS):
            try:
                return await self._one_worker_call(expression, reload_first=attempt > 0)
            except Exception as exc:
                last = exc
                await asyncio.sleep(WORKER_RETRY_S)
        raise RuntimeError(f"extension call failed after {WORKER_ATTEMPTS} attempts: {last}")

    async def _one_worker_call(self, expression: str, *, reload_first: bool) -> Any:
        version = await self.json("/json/version")
        ids = itertools.count(1)
        async with self._connect(version["webSocketDebuggerUrl"]) as ws:
            extension_id = await self._extension_id(ws, ids, reload_first=reload_first)
            url = f"chrome-extension://{extension_id}/{shipped_worker()}"
            targets = await self._worker_targets(ws, ids, url)
            failure: BaseException | None = None
            # meiva:note
            # Newest target first: Chrome keeps the dead worker targets listed.
            for target in reversed(targets):
                try:
                    return await _attach_and_evaluate(ws, ids, target, expression)
                except Exception as exc:
                    failure = exc
            raise RuntimeError(f"no live worker answered: {failure}")

    async def _extension_id(self, ws: Any, ids: Any, *, reload_first: bool) -> str:
        loaded = [
            entry
            for entry in (await cdp_call(ws, ids, "Extensions.getExtensions", {}))["extensions"]
            if Path(entry["path"]).resolve() == EXTENSION_DIR.resolve()
        ]
        if reload_first or not loaded:
            loaded_id = (
                await cdp_call(ws, ids, "Extensions.loadUnpacked", {"path": str(EXTENSION_DIR)})
            )["id"]
            await asyncio.sleep(WORKER_RETRY_S)
            return str(loaded_id)
        return str(loaded[0]["id"])

    async def _worker_targets(self, ws: Any, ids: Any, url: str) -> list[str]:
        deadline = time.monotonic() + WORKER_SCRIPT_WAIT_S
        while True:
            infos = (await cdp_call(ws, ids, "Target.getTargets", {}))["targetInfos"]
            targets = [
                str(info["targetId"])
                for info in infos
                if info.get("type") == "service_worker" and info.get("url") == url
            ]
            if targets:
                return targets
            if time.monotonic() >= deadline:
                raise RuntimeError("the tab-group worker did not appear")
            await asyncio.sleep(0.1)


# meiva:note
# Attach to one worker target and evaluate `expression` there (raises on failure).
async def _attach_and_evaluate(ws: Any, ids: Any, target: str, expression: str) -> Any:
    session_id = (
        await asyncio.wait_for(
            cdp_call(ws, ids, "Target.attachToTarget", {"targetId": target, "flatten": True}),
            WORKER_TIMEOUT_S,
        )
    )["sessionId"]
    outcome = await asyncio.wait_for(
        cdp_call(
            ws,
            ids,
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            session_id=session_id,
        ),
        WORKER_TIMEOUT_S,
    )
    if "exceptionDetails" in outcome:
        raise RuntimeError(outcome["exceptionDetails"]["exception"]["description"])
    return outcome["result"].get("value")
