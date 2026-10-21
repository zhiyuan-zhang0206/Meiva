# meiva:note
# The browser session ledger: what a deliberate stop records and what a start restores.

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

from services.desktop.browser import session_restore
from services.desktop.browser.session_restore import (
    PENDING_LAYOUT,
    PENDING_RESTORE,
    Ledger,
    Tab,
    ledger_path,
)


# meiva:note
# The `Chrome` seam: scripted `/json/*` bodies and scripted worker answers.
class FakeChrome:
    def __init__(
        self,
        *,
        listing: list[dict[str, Any]] | Exception | None = None,
        layout: list[list[dict[str, Any]]] | Exception | None = None,
        worker_result: Any = None,
        worker_error: Exception | None = None,
    ) -> None:
        self.listing = listing
        self.layout = layout
        self.worker_result = worker_result
        self.worker_error = worker_error
        self.expressions: list[str] = []

    async def json(self, path: str) -> Any:
        assert path == "/json/list"
        if isinstance(self.listing, Exception):
            raise self.listing
        return self.listing

    async def worker(self, expression: str) -> Any:
        self.expressions.append(expression)
        if self.worker_error is not None:
            raise self.worker_error
        if expression == "meivaLayout()":
            if isinstance(self.layout, Exception):
                raise self.layout
            return self.layout
        return self.worker_result


def _ledger(*tabs: Tab, layout: bool = True, pending: str | None = PENDING_RESTORE) -> Ledger:
    return Ledger(
        tabs=tuple(tabs),
        captured_at="2026-10-22T00:00:00+00:00",
        layout=layout,
        pending=pending,
        reapply=None,
        report_pending=True,
    )


def _tab(url: str, *, pinned: bool = False, group: str | None = None, index: int = 0) -> Tab:
    return Tab(url=url, title="", pinned=pinned, group=group, index=index, window=0)


# meiva:note
# ── the ledger file ─────────────────────────────────────────────────────────


def test_ledger_round_trips_and_is_private(tmp_path: Path) -> None:
    ledger = _ledger(
        _tab("https://a.example/", pinned=True),
        _tab("https://b.example/", index=1, group="Meiva #7"),
    )
    session_restore.write(tmp_path, ledger)
    path = ledger_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert session_restore.read(tmp_path) == ledger


def test_a_missing_or_broken_ledger_reads_as_no_record(tmp_path: Path) -> None:
    assert session_restore.read(tmp_path) is None
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert session_restore.read(tmp_path) is None
    path.write_text(json.dumps({"tabs": "not a list"}))
    assert session_restore.read(tmp_path) is None


# meiva:note
# Only the shape the restore needs: URL, title, layout, position.
def test_the_ledger_never_carries_a_secret(tmp_path: Path) -> None:
    session_restore.write(tmp_path, _ledger(_tab("https://a.example/?q=1", pinned=True)))
    stored = json.loads(ledger_path(tmp_path).read_text())
    assert set(stored) == {
        "captured_at",
        "layout",
        "pending",
        "reapply",
        "report_pending",
        "tabs",
    }
    assert set(stored["tabs"][0]) == {"url", "title", "pinned", "group", "index", "window"}


# meiva:note
# ── the stop -> start handshake ─────────────────────────────────────────────


def test_begin_launch_consumes_a_pending_restore_exactly_once(tmp_path: Path) -> None:
    session_restore.write(tmp_path, _ledger(_tab("https://a.example/")))
    assert session_restore.begin_launch(tmp_path) is True
    stored = session_restore.read(tmp_path)
    assert stored is not None and stored.pending == PENDING_LAYOUT
    # meiva:note
    # A second launch must not replay the same session.
    assert session_restore.begin_launch(tmp_path) is False


def test_begin_launch_without_a_record_asks_for_nothing(tmp_path: Path) -> None:
    assert session_restore.begin_launch(tmp_path) is False


# meiva:note
# `meiva start --only-service gateway` must not spend the recorded session.
def test_a_pending_restore_that_never_launched_stays_pending(tmp_path: Path) -> None:
    session_restore.write(tmp_path, _ledger(_tab("https://a.example/")))
    assert session_restore.report(home=tmp_path, chrome=FakeChrome(listing=[])) is None
    stored = session_restore.read(tmp_path)
    assert stored is not None and stored.pending == PENDING_RESTORE


# meiva:note
# ── capture ────────────────────────────────────────────────────────────────


async def test_capture_records_pins_and_groups_from_the_worker() -> None:
    chrome = FakeChrome(
        layout=[
            [
                {
                    "index": 0,
                    "url": "https://a.example/",
                    "title": "A",
                    "pinned": True,
                    "group": None,
                },
                {
                    "index": 1,
                    "url": "https://b.example/",
                    "title": "B",
                    "pinned": False,
                    "group": "Meiva #7",
                },
            ]
        ]
    )
    ledger = await session_restore.capture(chrome)
    assert ledger is not None
    assert [tab.url for tab in ledger.tabs] == ["https://a.example/", "https://b.example/"]
    assert ledger.tabs[0].pinned is True
    assert ledger.tabs[1].group == "Meiva #7"
    assert ledger.layout is True
    assert ledger.pending == PENDING_RESTORE


async def test_capture_falls_back_to_urls_when_the_worker_is_unavailable() -> None:
    chrome = FakeChrome(
        layout=RuntimeError("no live worker answered"),
        listing=[
            {"type": "page", "url": "https://a.example/"},
            {"type": "service_worker", "url": "chrome-extension://x/w.js"},
            {"type": "page", "url": "https://b.example/"},
        ],
    )
    ledger = await session_restore.capture(chrome)
    assert ledger is not None
    assert [tab.url for tab in ledger.tabs] == ["https://a.example/", "https://b.example/"]
    assert ledger.layout is False
    assert all(not tab.pinned and tab.group is None for tab in ledger.tabs)


async def test_capture_without_a_browser_records_nothing() -> None:
    chrome = FakeChrome(layout=RuntimeError("no worker"), listing=OSError("connection refused"))
    assert await session_restore.capture(chrome) is None


# meiva:note
# ── the reapply the flag cannot promise ────────────────────────────────────


def test_layout_spec_carries_pins_and_groups_in_recorded_order() -> None:
    ledger = _ledger(
        _tab("https://a.example/", pinned=True, index=0),
        _tab("https://b.example/", index=1, group="Meiva #7"),
        _tab("https://c.example/", index=2, group="Meiva #7"),
        _tab("https://d.example/", index=3),
    )
    assert session_restore.layout_spec(ledger) == {
        "pins": ["https://a.example/"],
        "groups": [{"title": "Meiva #7", "urls": ["https://b.example/", "https://c.example/"]}],
    }
    # meiva:note
    # One pin plus two grouped tabs.
    assert ledger.layout_expected == 3


async def test_reapply_records_what_it_applied_and_clears_the_pending_flag(
    tmp_path: Path,
) -> None:
    session_restore.write(
        tmp_path,
        _ledger(_tab("https://a.example/", pinned=True), pending=PENDING_LAYOUT),
    )
    chrome = FakeChrome(worker_result={"pinned": 1, "grouped": 0})
    await session_restore.reapply(tmp_path, chrome)
    stored = session_restore.read(tmp_path)
    assert stored is not None
    assert stored.pending is None
    assert stored.reapply == {"applied": 1, "expected": 1}
    assert chrome.expressions[0].startswith("meivaApplyLayout(")


async def test_reapply_failure_degrades_to_the_restored_tabs(tmp_path: Path) -> None:
    session_restore.write(
        tmp_path,
        _ledger(_tab("https://a.example/", pinned=True), pending=PENDING_LAYOUT),
    )
    await session_restore.reapply(tmp_path, FakeChrome(worker_error=RuntimeError("no worker")))
    stored = session_restore.read(tmp_path)
    assert stored is not None
    assert stored.pending is None
    assert stored.reapply == {"applied": 0, "expected": 1}
    # meiva:note
    # The record of what was open survives the failed reapply.
    assert [tab.url for tab in stored.tabs] == ["https://a.example/"]


async def test_reapply_is_a_no_op_without_a_pending_layout(tmp_path: Path) -> None:
    session_restore.write(tmp_path, _ledger(_tab("https://a.example/"), pending=None))
    chrome = FakeChrome(worker_result={})
    await session_restore.reapply(tmp_path, chrome)
    assert chrome.expressions == []


# meiva:note
# ── the self-proof line ────────────────────────────────────────────────────


def _report_ledger(tmp_path: Path, tabs: list[Tab], *, layout: bool = True) -> None:
    expected = sum(1 for tab in tabs if tab.pinned or tab.group)
    session_restore.write(
        tmp_path,
        Ledger(
            tabs=tuple(tabs),
            captured_at="2026-10-22T00:00:00+00:00",
            layout=layout,
            pending=PENDING_LAYOUT,
            reapply={"applied": expected, "expected": expected},
            report_pending=True,
        ),
    )


# meiva:note
# One number would hide the loss shape this feature exists to repair.
def test_report_counts_urls_and_layout_separately(tmp_path: Path) -> None:
    _report_ledger(
        tmp_path,
        [_tab("https://a.example/", pinned=True), _tab("https://b.example/", index=1)],
    )
    chrome = FakeChrome(
        listing=[
            {"type": "page", "url": "https://a.example/"},
            {"type": "page", "url": "https://b.example/"},
            {"type": "page", "url": "about:blank"},
        ]
    )
    assert session_restore.report(home=tmp_path, chrome=chrome, wait_s=0) == (
        "browser session: restored 2/2 tabs; pin/group reapplied 1/1"
    )


def test_report_never_counts_the_browsers_own_blank_tab(tmp_path: Path) -> None:
    _report_ledger(tmp_path, [_tab("https://a.example/")])
    chrome = FakeChrome(
        listing=[
            {"type": "page", "url": "about:blank"},
            {"type": "page", "url": "https://a.example/"},
        ]
    )
    assert session_restore.report(home=tmp_path, chrome=chrome, wait_s=0) == (
        "browser session: restored 1/1 tabs; pin/group reapplied 0/0"
    )


def test_report_counts_a_url_once_per_tab(tmp_path: Path) -> None:
    _report_ledger(tmp_path, [_tab("https://a.example/"), _tab("https://a.example/", index=1)])
    chrome = FakeChrome(listing=[{"type": "page", "url": "https://a.example/"}])
    assert session_restore.report(home=tmp_path, chrome=chrome, wait_s=0) == (
        "browser session: restored 1/2 tabs; pin/group reapplied 0/0"
    )


def test_report_says_so_when_the_layout_was_never_recorded(tmp_path: Path) -> None:
    _report_ledger(tmp_path, [_tab("https://a.example/")], layout=False)
    chrome = FakeChrome(listing=[{"type": "page", "url": "https://a.example/"}])
    assert session_restore.report(home=tmp_path, chrome=chrome, wait_s=0) == (
        "browser session: restored 1/1 tabs; pin/group not recorded (the worker was unavailable at stop)"
    )


def test_report_is_printed_once(tmp_path: Path) -> None:
    _report_ledger(tmp_path, [_tab("https://a.example/")])
    chrome = FakeChrome(listing=[{"type": "page", "url": "https://a.example/"}])
    assert session_restore.report(home=tmp_path, chrome=chrome, wait_s=0) is not None
    assert session_restore.report(home=tmp_path, chrome=chrome, wait_s=0) is None


# meiva:note
# ── the stop-side entry point ──────────────────────────────────────────────


def test_record_for_stop_writes_a_restorable_ledger(tmp_path: Path) -> None:
    chrome = FakeChrome(
        layout=[
            [
                {
                    "index": 0,
                    "url": "https://a.example/",
                    "title": "",
                    "pinned": True,
                    "group": None,
                },
                {
                    "index": 1,
                    "url": "https://b.example/",
                    "title": "",
                    "pinned": False,
                    "group": "Meiva #7",
                },
            ]
        ]
    )
    line = session_restore.record_for_stop(home=tmp_path, chrome=chrome)
    stored = session_restore.read(tmp_path)
    assert stored is not None and stored.pending == PENDING_RESTORE
    assert line == "browser session: recorded 2 tab(s) (1 pinned, 1 tab group(s))"


def test_record_for_stop_never_raises_and_records_nothing_without_a_browser(
    tmp_path: Path,
) -> None:
    chrome = FakeChrome(layout=RuntimeError("no worker"), listing=OSError("no browser"))
    line = session_restore.record_for_stop(home=tmp_path, chrome=chrome)
    assert session_restore.read(tmp_path) is None
    assert line is not None and "nothing to record" in line


# meiva:note
# ── the worker ladder ──────────────────────────────────────────────────────


# meiva:note
# One browser-level CDP websocket: replies to the methods the ladder sends.
class FakeCdpWs:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.pending: list[str] = []
        self.expressions: list[str] = []

    async def __aenter__(self) -> FakeCdpWs:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def send(self, raw: str) -> None:
        request = json.loads(raw)
        method = request["method"]
        if method == "Extensions.getExtensions":
            found = (
                [{"id": "EXT", "path": str(session_restore.EXTENSION_DIR)}]
                if self.state["loaded"]
                else []
            )
            self.pending.append(json.dumps({"id": request["id"], "result": {"extensions": found}}))
        elif method == "Extensions.loadUnpacked":
            self.state["loaded"] = True
            self.state["live"] = True
            self.pending.append(json.dumps({"id": request["id"], "result": {"id": "EXT"}}))
        elif method == "Target.getTargets":
            url = f"chrome-extension://EXT/{session_restore.shipped_worker()}"
            infos = [{"type": "service_worker", "url": url, "targetId": "DEAD"}]
            if self.state["live"]:
                infos.append({"type": "service_worker", "url": url, "targetId": "LIVE"})
            self.pending.append(json.dumps({"id": request["id"], "result": {"targetInfos": infos}}))
        elif method == "Target.attachToTarget":
            target = request["params"]["targetId"]
            if target == "DEAD":
                self.pending.append(
                    json.dumps(
                        {
                            "id": request["id"],
                            "error": {"code": -32602, "message": "No target with given id found"},
                        }
                    )
                )
            else:
                self.pending.append(
                    json.dumps({"id": request["id"], "result": {"sessionId": "S1"}})
                )
        elif method == "Runtime.evaluate":
            self.expressions.append(request["params"]["expression"])
            self.pending.append(
                json.dumps({"id": request["id"], "result": {"result": {"value": {"ok": True}}}})
            )
        else:
            raise AssertionError(method)

    async def recv(self) -> str:
        return self.pending.pop(0)


# meiva:note
# A worker that idled out stays listed but refuses attach; only a reload
#     brings a live one back (measured on Chrome 156).
async def test_the_ladder_reloads_the_extension_after_a_dead_worker_refuses_attach() -> None:
    state = {"loaded": True, "live": False}
    sockets: list[FakeCdpWs] = []

    def connect(_url: str) -> FakeCdpWs:
        ws = FakeCdpWs(state)
        sockets.append(ws)
        return ws

    async def fetch(_url: str) -> Any:
        return {"webSocketDebuggerUrl": "ws://browser"}

    chrome = session_restore.CdpChrome(0, fetch=fetch, connect=connect)
    assert await chrome.worker("meivaLayout()") == {"ok": True}
    # meiva:note
    # The first attempt hit the dead target; the reload answered.
    assert len(sockets) == 2
    assert sockets[1].expressions == ["meivaLayout()"]


# meiva:note
# The reapply only ever pins and groups: restoring what was there must never
#     close, move or unpin anything the user has open.
def test_the_shipped_worker_reports_and_re_applies_the_layout_without_destroying_anything() -> None:
    worker = next(session_restore.EXTENSION_DIR.glob("group-*.js")).read_text()
    assert "self.meivaLayout" in worker
    assert "self.meivaApplyLayout" in worker
    assert "chrome.tabs.update(tab.id, { pinned: true })" in worker
    assert "chrome.tabs.group" in worker
    assert "tabGroups.query({ title: group.title })" in worker
    for forbidden in ("tabs.remove", "tabGroups.remove", "pinned: false", "tabs.move"):
        assert forbidden not in worker, forbidden


def test_report_still_prints_when_the_daemon_finished_first(tmp_path: Path) -> None:
    # meiva:note
    # The reapply runs in browser-mcp and can finish before this line runs; the
    # report is keyed on `report_pending`, not on the handshake field, or a fast
    # daemon would swallow the proof line entirely.
    expected = 1
    session_restore.write(
        tmp_path,
        Ledger(
            tabs=(_tab("https://a.example/", pinned=True),),
            captured_at="2026-10-22T00:00:00+00:00",
            layout=True,
            pending=None,
            reapply={"applied": expected, "expected": expected},
            report_pending=True,
        ),
    )
    chrome = FakeChrome(listing=[{"type": "page", "url": "https://a.example/"}])
    assert session_restore.report(home=tmp_path, chrome=chrome, wait_s=0) == (
        "browser session: restored 1/1 tabs; pin/group reapplied 1/1"
    )
