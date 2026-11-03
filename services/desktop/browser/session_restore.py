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
import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from base.clock import Timers
from base.host.atomic_io import write_text_atomic
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.log import logger
from base.paths import run_dir

from .session import http_json
from .tab_groups.grouping import (
    EXTENSION_DIR as EXTENSION_DIR,
)
from .tab_groups.grouping import (
    ExtensionWorker,
    staged_extension_dir,
)
from .tab_groups.grouping import (
    shipped_worker as shipped_worker,
)

LEDGER_NAME = "browser-session.json"

# meiva:note
# A local `/json/list` answers in milliseconds; the bound only covers a wedged
# Chrome, and the stop phase that calls this must never hold the shutdown open.
LISTING_TIMEOUT_S = 2.0

# meiva:note
# Bound on one service-worker call (waking an idled-out worker via `loadUnpacked`
# plus evaluating the layout helper). Every caller treats a failure as degrade,
# never as a failed stop or a failed start.
WORKER_TIMEOUT_S = 10.0

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


# meiva:note
# The two reads this module needs from a browser.
class Chrome(Protocol):
    # meiva:note
    # GET one of CDP's HTTP endpoints (`/json/list`, `/json/version`).
    async def json(self, path: str) -> object: ...

    # meiva:note
    # Evaluate `expression` in the tab-group extension's worker and return its value.
    async def worker(self, expression: str) -> object: ...


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
# A recorded number as `int(...)` reads it: a number or a numeric string; anything else is malformed.
def _whole(value: object) -> int:
    if isinstance(value, int | float | str):
        return int(value)
    raise TypeError("a recorded number must be a number or a numeric string")


def _parse_tab(item: dict[str, object]) -> Tab:
    group_val = item.get("group")
    if group_val is not None and not isinstance(group_val, str):
        raise TypeError("tab group must be a string or None")
    return Tab(
        url=str(item["url"]),
        title=str(item.get("title", "")),
        pinned=bool(item.get("pinned", False)),
        group=group_val,
        index=_whole(item.get("index", 0)),
        window=_whole(item.get("window", 0)),
    )


def _parse_reapply(raw: dict[str, object] | None) -> dict[str, int] | None:
    if raw is None:
        return None
    return {str(k): _whole(v) for k, v in raw.items()}


def _parse_ledger_payload(raw: dict[str, object]) -> Ledger:
    raw_tabs = raw["tabs"]
    if not is_object_list(raw_tabs):
        raise TypeError("tabs must be a list")
    tabs_list: list[dict[str, object]] = []
    for entry in raw_tabs:
        if not is_object_dict(entry):
            raise TypeError("tab entry must be a dict")
        tabs_list.append(entry)
    raw_pending = raw.get("pending")
    if raw_pending is not None and not isinstance(raw_pending, str):
        raise TypeError("pending must be a string or None")
    raw_reapply = raw.get("reapply")
    if raw_reapply is not None and not is_object_dict(raw_reapply):
        raise TypeError("reapply must be a dict")
    tabs = tuple(_parse_tab(item) for item in tabs_list)
    return Ledger(
        tabs=tabs,
        captured_at=str(raw.get("captured_at", "")),
        layout=bool(raw.get("layout", False)),
        pending=raw_pending,
        reapply=_parse_reapply(raw_reapply),
        report_pending=bool(raw.get("report_pending", False)),
    )


# meiva:note
# The ledger, or None when this unit never recorded one (or it is unreadable).
def read(home: Path) -> Ledger | None:
    path = ledger_path(home)
    try:
        raw = object_of(json.loads, path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("[browser-session] ignoring unreadable {}: {}", path, exc)
        return None
    if not is_object_dict(raw):
        logger.warning("[browser-session] ignoring malformed ledger {}", path)
        return None
    try:
        return _parse_ledger_payload(raw)
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
async def capture(chrome: Chrome, *, now: datetime) -> Ledger | None:
    captured_at = now.isoformat(timespec="seconds")
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
            captured_at=captured_at,
            layout=False,
            pending=PENDING_RESTORE,
            reapply=None,
            report_pending=True,
        )
    tabs: list[Tab] = []
    for window_index, window in enumerate(windows):
        for entry in window:
            group = entry.get("group")
            tabs.append(
                Tab(
                    url=str(entry.get("url", "")),
                    title=str(entry.get("title", "")),
                    pinned=bool(entry.get("pinned", False)),
                    group=group if isinstance(group, str) else None,
                    index=_whole(entry.get("index", 0)),
                    window=window_index,
                )
            )
    if not any(tab.url for tab in tabs):
        return None
    return Ledger(
        tabs=tuple(tabs),
        captured_at=captured_at,
        layout=True,
        pending=PENDING_RESTORE,
        reapply=None,
        report_pending=True,
    )


# meiva:note
# Every window's tabs as the extension sees them, or None when it cannot be asked.
async def _extension_layout(chrome: Chrome) -> list[list[dict[str, object]]] | None:
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
    if not is_object_list(raw):
        return None
    windows: list[list[dict[str, object]]] = []
    for window in raw:
        if not is_object_list(window):
            continue
        windows.append([row for row in window if is_object_dict(row)])
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
    if not is_object_list(listing):
        return None
    urls: list[str] = []
    for target in listing:
        if is_object_dict(target) and target.get("type") == "page":
            urls.append(str(target.get("url", "")))
    return urls


# meiva:note
# ── re-applying the layout the flag can drop ───────────────────────────────


# meiva:note
# The extension's `meivaApplyLayout` argument: pins first, then groups by title.
def layout_spec(ledger: Ledger) -> dict[str, object]:
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
    counts: dict[str, object] = outcome if is_object_dict(outcome) else {}
    applied = _whole(counts.get("pinned", 0)) + _whole(counts.get("grouped", 0))
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
def report(
    *,
    home: Path,
    chrome: Chrome,
    timers: Timers,
    wait_s: float = REPORT_WAIT_S,
) -> str | None:
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
    deadline = timers.monotonic() + wait_s
    while ledger.pending == PENDING_LAYOUT and timers.monotonic() < deadline:
        timers.sleep(REPORT_POLL_S)
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
def record_for_stop(*, home: Path, chrome: Chrome, now: datetime) -> str | None:
    try:
        ledger = asyncio.run(capture(chrome, now=now))
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


# meiva:note
# `Chrome` over this unit's CDP port, delegating service-worker evaluation to an
# `ExtensionWorker` so layout capture/reapply shares the staged extension
# directory, stale-copy cleanup and worker lock.
class CdpChrome:
    def __init__(
        self,
        cdp_port: int,
        *,
        worker: ExtensionWorker,
    ) -> None:
        self._cdp_port = cdp_port
        self._worker = worker

    @classmethod
    def for_home(
        cls,
        cdp_port: int,
        *,
        home: Path,
        timers: Timers,
    ) -> CdpChrome:
        return cls(
            cdp_port,
            worker=ExtensionWorker(
                cdp_port,
                timers=timers,
                extension_dir=staged_extension_dir(home),
            ),
        )

    async def json(self, path: str) -> object:
        url = f"http://127.0.0.1:{self._cdp_port}{path}"
        return await asyncio.wait_for(http_json(url), LISTING_TIMEOUT_S)

    async def worker(self, expression: str) -> object:
        return await self._worker.evaluate(expression, timeout_s=WORKER_TIMEOUT_S)
