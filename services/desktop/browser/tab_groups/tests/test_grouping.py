"""Per-agent tab groups: the CDP client, the extension files and the failure policy.

Grouping is presentation only. A failure to group must leave the page open and
usable and leave a WARNING behind -- never an exception into the browser
operation, never silence (see
docs/decisions/engineering/design/contracts/2026-10-12-presentation-only-failure-degrades-with-warning.md).
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
import websockets.asyncio.server
from websockets.asyncio.server import ServerConnection

from base.clock import SYSTEM_TIMERS
from services.desktop.browser.tab_groups import grouping
from services.desktop.browser.tab_groups.grouping import (
    EXTENSION_DIR,
    AgentTabGroups,
    group_title,
)

EXTENSION_ID = "ext0123456789abcdefghijklmnopabcd"


# meiva:note
# Recorded CDP websocket state for assertions.
class FakeCdpWS:
    def __init__(self, handlers: dict[str, Any]) -> None:
        self.handlers = handlers
        self.sent: list[dict[str, Any]] = []

    def methods(self) -> list[str]:
        return [r["method"] for r in self.sent]


def _page(target_id: str, url: str = "https://example.com/") -> dict[str, str]:
    return {"id": target_id, "type": "page", "url": url}


SHIPPED_WORKER = next(EXTENSION_DIR.glob("group-*.js")).name


class ChromeModel:
    """The extension-relevant state of one Chrome, as CDP handlers.

    ``loaded``: the extension is loaded in this Chrome session (False on a fresh
    or restarted Chrome); ``worker``: the file name of its running service worker
    or None when it idled out. ``loadUnpacked`` loads the extension and starts the
    shipped worker, like Chrome does.
    """

    def __init__(
        self,
        *,
        loaded: bool = False,
        worker: str | None = None,
        script_delay: int = 0,
        ungrouped: bool = True,
        report: dict[str, Any] | None = None,
        converge: list[dict[str, Any]] | None = None,
        converge_error: Exception | None = None,
    ) -> None:
        self.loaded = loaded
        self.worker = worker
        self.script_delay = script_delay  # probes answering "undefined" before the script ran
        self.ungrouped = ungrouped
        # meiva:note
        # What the worker reports for `meivaGroup` (a convergence of one window) and
        # for `meivaConvergeAll` (one report per group it found). A test sets the
        # merge it wants to see reported, or the failure `converge_all` must degrade.
        # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
        self.report = report or {
            "title": "Meiva #7",
            "windowId": 1,
            "groups": [11],
            "kept": 11,
            "merged": 0,
            "moved": 0,
            "created": False,
        }
        self.converge = converge if converge is not None else []
        self.converge_error = converge_error
        self.expressions: list[str] = []

    def handlers(self) -> dict[str, Any]:
        return {
            "Extensions.getExtensions": self._get_extensions,
            "Extensions.loadUnpacked": self._load_unpacked,
            "Target.getTargets": self._get_targets,
            "Target.attachToTarget": {"sessionId": "S1"},
            "Runtime.evaluate": self._evaluate,
        }

    def _evaluate(self, params: dict[str, Any]) -> dict[str, Any] | Exception:
        self.expressions.append(params["expression"])
        if params["expression"] == "typeof meivaGroup":
            if self.script_delay > 0:
                self.script_delay -= 1
                return {"result": {"type": "string", "value": "undefined"}}
            return {"result": {"type": "string", "value": "function"}}
        if params["expression"].startswith("meivaUngroupLast("):
            return {"result": {"type": "boolean", "value": self.ungrouped}}
        if params["expression"] == "meivaConvergeAll()":
            if self.converge_error is not None:
                return self.converge_error
            return {"result": {"type": "object", "value": self.converge}}
        return {"result": {"type": "object", "value": self.report}}

    def _get_extensions(self, _params: dict[str, Any]) -> dict[str, Any]:
        found = [{"id": EXTENSION_ID, "path": str(EXTENSION_DIR)}] if self.loaded else []
        return {"extensions": found}

    def _load_unpacked(self, params: dict[str, Any]) -> dict[str, Any]:
        assert params == {"path": str(EXTENSION_DIR)}
        self.loaded, self.worker = True, SHIPPED_WORKER
        return {"id": EXTENSION_ID}

    def _get_targets(self, _params: dict[str, Any]) -> dict[str, Any]:
        infos = [{"type": "page", "url": "https://example.com/", "targetId": "P1"}]
        if self.worker is not None:
            url = f"chrome-extension://{EXTENSION_ID}/{self.worker}"
            infos.append({"type": "service_worker", "url": url, "targetId": "SW1"})
        return {"targetInfos": infos}


def _chrome_handlers() -> dict[str, Any]:
    return ChromeModel().handlers()


# meiva:note
# Loopback HTTP + websocket surfaces of one Chrome.
class FakeChrome:
    def __init__(
        self,
        handlers: dict[str, Any],
        *,
        wedged_http: bool = False,
        wedged_ws: bool = False,
        racy_groups: list[str] | None = None,
    ) -> None:
        self.listings: list[Any] = []
        self.ws = FakeCdpWS(handlers)
        self.http_urls: list[str] = []
        self.port: int = 0
        self.ws_port: int = 0
        self.wedged_http = wedged_http
        self.wedged_ws = wedged_ws
        self.racy_groups = racy_groups
        self.ws_entered = asyncio.Event()
        self._unwedge = asyncio.Event()
        self._http_server: asyncio.Server | None = None
        self._ws_server: websockets.asyncio.server.Server | None = None

    @classmethod
    async def start(
        cls,
        handlers: dict[str, Any],
        *,
        wedged_http: bool = False,
        wedged_ws: bool = False,
        racy_groups: list[str] | None = None,
    ) -> FakeChrome:
        chrome = cls(
            handlers,
            wedged_http=wedged_http,
            wedged_ws=wedged_ws,
            racy_groups=racy_groups,
        )
        ws_server = await websockets.asyncio.server.serve(chrome._handle_ws, "127.0.0.1", 0)
        chrome._ws_server = ws_server
        ws_sock = next(iter(ws_server.sockets))
        ws_addr: tuple[str, int] = ws_sock.getsockname()
        chrome.ws_port = ws_addr[1]
        http_server = await asyncio.start_server(chrome._handle_http, "127.0.0.1", 0)
        chrome._http_server = http_server
        http_sock = next(iter(http_server.sockets or ()))
        http_addr: tuple[str, int] = http_sock.getsockname()
        chrome.port = http_addr[1]
        _CLEANUPS.append(chrome)
        return chrome

    async def close(self) -> None:
        self._unwedge.set()
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        if self._http_server is not None:
            self._http_server.close()
            await self._http_server.wait_closed()

    def groups(
        self,
        *,
        listing_timeout_s: float = grouping.LISTING_TIMEOUT_S,
        group_timeout_s: float = grouping.GROUP_TIMEOUT_S,
        worker_wait_s: float = grouping.WORKER_WAIT_S,
    ) -> AgentTabGroups:
        return AgentTabGroups(
            self.port,
            _TASK_GROUPS[0],
            listing_timeout_s=listing_timeout_s,
            group_timeout_s=group_timeout_s,
            worker_wait_s=worker_wait_s,
            timers=SYSTEM_TIMERS,
        )

    async def _handle_http(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            first_line = raw.decode("latin-1").splitlines()[0]
            path = first_line.split(" ")[1]
            self.http_urls.append(f"http://127.0.0.1:{self.port}{path}")
            if self.wedged_http:
                await self._unwedge.wait()
                return
            if path.endswith("/json/version"):
                payload = {
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.ws_port}/devtools/browser"
                }
            else:
                outcome = self.listings.pop(0)
                if isinstance(outcome, Exception):
                    return
                payload = outcome
            body = json.dumps(payload).encode("utf-8")
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("latin-1")
            writer.write(header + body)
            await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _handle_ws(self, conn: ServerConnection) -> None:
        self.ws_entered.set()
        if self.wedged_ws:
            await self._unwedge.wait()
            return
        with suppress(websockets.ConnectionClosed):
            async for raw_msg in conn:
                data = raw_msg if isinstance(raw_msg, str) else raw_msg.decode("utf-8")
                request = json.loads(data)
                self.ws.sent.append(request)
                exists: bool | None = None
                title = ""
                if self.racy_groups is not None:
                    match = re.fullmatch(
                        r'meivaGroup\("[^"]+", "([^"]+)"\)',
                        request.get("params", {}).get("expression", ""),
                    )
                    if request["method"] == "Runtime.evaluate" and match:
                        title = match.group(1)
                        exists = title in self.racy_groups
                handler = self.ws.handlers[request["method"]]
                outcome = handler(request.get("params", {})) if callable(handler) else handler
                reply: dict[str, Any] = {"id": request["id"]}
                if isinstance(outcome, Exception):
                    reply["error"] = {"code": -32000, "message": str(outcome)}
                else:
                    reply["result"] = outcome
                await conn.send(json.dumps({"method": "Target.targetInfoChanged", "params": {}}))
                if exists is not None and self.racy_groups is not None:
                    await asyncio.sleep(0.02)
                    if not exists:
                        self.racy_groups.append(title)
                await conn.send(json.dumps(reply))


_TASK_GROUPS: list[asyncio.TaskGroup] = []
_CLEANUPS: list[FakeChrome] = []


@pytest.fixture(autouse=True)
async def _daemon_task_group() -> AsyncIterator[None]:
    """The daemon's root TaskGroup, which owns the background grouping tasks."""
    try:
        async with asyncio.TaskGroup() as tg:
            _TASK_GROUPS.append(tg)
            yield
    finally:
        _TASK_GROUPS.clear()
        while _CLEANUPS:
            await _CLEANUPS.pop().close()


async def _group(
    groups: AgentTabGroups,
    agent_id: int,
    before: frozenset[str] | None,
    page_id: int | None = None,
) -> None:
    """`group_new_page`, then wait for the background grouping it started."""
    await groups.group_new_page(agent_id, before, page_id)
    await asyncio.gather(*groups._tasks)


def _warnings(records: list[dict[str, Any]]) -> list[str]:
    return [r["message"] for r in records if r["level"].name == "WARNING"]


# ── The group title ──────────────────────────────────────────────────────────


def test_group_title_identifies_the_agent_and_only_the_agent() -> None:
    assert group_title(405) == "Meiva #405"
    assert len({group_title(i) for i in range(1, 50)}) == 49  # unique per agent id


# ── The extension that ships with the package ───────────────────────────────


def test_worker_filename_carries_its_content_hash() -> None:
    """Chrome caches an extension's service-worker script in the profile and keeps
    running the cached copy when only the content changes (measured on Chrome 153;
    a bumped manifest version does not help). Only a new FILE NAME reliably
    loads new code, so the name must follow the content."""
    workers = sorted(EXTENSION_DIR.glob("group-*.js"))
    assert len(workers) == 1
    digest = hashlib.sha256(workers[0].read_bytes()).hexdigest()[:12]
    assert workers[0].name == f"group-{digest}.js", (
        f"worker content changed: rename it to group-{digest}.js and update "
        "extension/manifest.json background.service_worker"
    )


def test_manifest_points_at_the_worker_and_asks_only_for_what_it_uses() -> None:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
    worker = next(EXTENSION_DIR.glob("group-*.js"))
    assert manifest["manifest_version"] == 3
    assert manifest["background"] == {"service_worker": worker.name}
    assert sorted(manifest["permissions"]) == ["debugger", "tabGroups", "tabs"]
    assert sorted(p.name for p in EXTENSION_DIR.iterdir()) == sorted(["manifest.json", worker.name])


def test_worker_is_stateless_and_only_ever_ungroups_an_agents_last_tab() -> None:
    # meiva:note
    # The group is found by title (no groupId survives anywhere), Chrome picks the
    # color, and the worker's one write that can destroy a group is
    # `meivaUngroupLast`: it must check the title AND that the group has no other
    # member, so a group the user made is never touched. There is still no delete
    # and no storage.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    source = next(EXTENSION_DIR.glob("group-*.js")).read_text()
    assert not re.search(r"\bcolor\s*:", source)
    assert not re.search(r"tabs\.remove|tabGroups\.remove|storage\.", source)
    assert "self.meivaUngroupLast" in source
    assert "group.title !== title" in source
    assert "members.length !== 1" in source
    # meiva:note
    # The ungroup is the last thing the function does, and nothing else calls it.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    assert source.count("tabs.ungroup") == 1


# ── snapshot: which page targets exist right now ───────────────────────────


async def test_snapshot_lists_page_targets_only() -> None:
    chrome = await FakeChrome.start({})
    chrome.listings.append(
        [
            _page("T1"),
            {"id": "W1", "type": "service_worker", "url": "chrome-extension://x/sw.js"},
            {"id": "X1", "type": "other", "url": "devtools://devtools/"},
            _page("T2"),
        ]
    )
    assert await chrome.groups().snapshot() == frozenset({"T1", "T2"})
    assert chrome.http_urls == [f"http://127.0.0.1:{chrome.port}/json/list"]


async def test_snapshot_failure_warns_and_returns_none(
    loguru_records: list[dict[str, Any]],
) -> None:
    chrome = await FakeChrome.start({})
    chrome.listings.append(OSError("connection refused"))
    assert await chrome.groups().snapshot() is None
    messages = _warnings(loguru_records)
    assert len(messages) == 1 and "tab group" in messages[0]


# meiva:note
# group_new_page: the CDP conversation


async def test_group_new_page_files_the_new_target_into_the_agent_group(
    loguru_records: list[dict[str, Any]],
) -> None:
    chrome = await FakeChrome.start(_chrome_handlers())
    chrome.listings.append([_page("T1"), _page("T2")])

    await _group(chrome.groups(), 7, frozenset({"T1"}))

    ws = chrome.ws
    assert ws.methods() == [
        "Extensions.getExtensions",
        "Extensions.loadUnpacked",
        "Target.getTargets",
        "Target.attachToTarget",
        "Runtime.evaluate",  # waits for the worker script to define meivaGroup
        "Runtime.evaluate",
    ]
    assert ws.sent[1]["params"] == {"path": str(EXTENSION_DIR)}
    assert ws.sent[3]["params"] == {"targetId": "SW1", "flatten": True}
    evaluate = ws.sent[5]
    assert evaluate["sessionId"] == "S1"
    assert evaluate["params"] == {
        "expression": 'meivaGroup("T2", "Meiva #7")',
        "awaitPromise": True,
        "returnByValue": True,
    }
    assert _warnings(loguru_records) == []


async def test_waits_for_the_worker_script_to_run_after_a_fresh_load(
    loguru_records: list[dict[str, Any]],
) -> None:
    """On first load the worker target can be attachable before its script ran
    (measured: `meivaGroup is not defined`), so the call waits for it."""
    model = ChromeModel(script_delay=3)
    chrome = await FakeChrome.start(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(chrome.groups(), 7, frozenset({"T1"}))
    evaluations = [
        r["params"]["expression"] for r in chrome.ws.sent if r["method"] == "Runtime.evaluate"
    ]
    assert evaluations == ["typeof meivaGroup"] * 4 + ['meivaGroup("T2", "Meiva #7")']
    assert _warnings(loguru_records) == []


async def test_a_worker_that_never_defines_the_function_warns(
    loguru_records: list[dict[str, Any]],
) -> None:
    chrome = await FakeChrome.start(ChromeModel(script_delay=10**6).handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(chrome.groups(worker_wait_s=0.0), 7, frozenset({"T1"}))
    messages = _warnings(loguru_records)
    assert len(messages) == 1 and "did not define meivaGroup" in messages[0]


async def test_a_live_worker_is_used_without_reloading_the_extension() -> None:
    """Reloading tears the running worker down; a call that attached to the dying
    target never gets its reply (measured). A live worker is therefore left alone."""
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER)
    chrome = await FakeChrome.start(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(chrome.groups(), 7, frozenset({"T1"}))
    assert "Extensions.loadUnpacked" not in chrome.ws.methods()
    assert chrome.ws.methods()[-1] == "Runtime.evaluate"


@pytest.mark.parametrize(
    ("model", "why"),
    [
        pytest.param(ChromeModel(), "fresh or restarted Chrome: not loaded", id="not-loaded"),
        pytest.param(
            ChromeModel(loaded=True, worker=None),
            "the worker idled out (~30s after its last event)",
            id="worker-idle",
        ),
        pytest.param(
            ChromeModel(loaded=True, worker="group-0000000000aa.js"),
            "an older worker file still runs (Chrome caches the worker script)",
            id="stale-worker-file",
        ),
    ],
)
async def test_extension_is_loaded_again_when_it_is_not_serving(
    model: ChromeModel, why: str
) -> None:
    chrome = await FakeChrome.start(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(chrome.groups(), 7, frozenset({"T1"}))
    assert chrome.ws.methods().count("Extensions.loadUnpacked") == 1, why
    assert chrome.ws.methods()[-1] == "Runtime.evaluate"
    assert model.worker == SHIPPED_WORKER


async def test_reconnect_needs_no_memory_of_an_earlier_load() -> None:
    """Two AgentTabGroups instances (a daemon restart) against one Chrome: the
    second finds the extension loaded and does not load it again; against a
    restarted Chrome it loads it."""
    model = ChromeModel()
    chrome = await FakeChrome.start(model.handlers())
    for _ in range(2):
        chrome.listings.append([_page("T1"), _page("T2")])
        await _group(chrome.groups(), 7, frozenset({"T1"}))
    assert chrome.ws.methods().count("Extensions.loadUnpacked") == 1
    model.loaded, model.worker = False, None  # Chrome restarted
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(chrome.groups(), 7, frozenset({"T1"}))
    assert chrome.ws.methods().count("Extensions.loadUnpacked") == 2


async def test_a_copy_of_the_extension_at_another_path_is_not_ours() -> None:
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER)
    handlers = model.handlers()
    handlers["Extensions.getExtensions"] = {
        "extensions": [{"id": EXTENSION_ID, "path": "/somewhere/else/extension"}]
    }
    chrome = await FakeChrome.start(handlers)
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(chrome.groups(), 7, frozenset({"T1"}))
    assert chrome.ws.methods().count("Extensions.loadUnpacked") == 1


async def test_group_new_page_skips_when_the_snapshot_failed(
    loguru_records: list[dict[str, Any]],
) -> None:
    """A failed snapshot was already reported; nothing to diff against."""
    chrome = await FakeChrome.start(_chrome_handlers())
    await _group(chrome.groups(), 7, None)
    assert chrome.ws.sent == [] and chrome.http_urls == []
    assert _warnings(loguru_records) == []


# meiva:note
# Failure policy: warn, never raise, never guess


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        pytest.param([_page("T1")], "0 new page targets", id="no-new-target"),
        pytest.param([_page("T1"), _page("T2"), _page("T3")], "2 new page targets", id="two"),
    ],
)
async def test_ambiguous_diff_warns_and_groups_nothing(
    after: list[dict[str, str]],
    expected: str,
    loguru_records: list[dict[str, Any]],
) -> None:
    chrome = await FakeChrome.start(_chrome_handlers())
    chrome.listings.append(after)
    await _group(chrome.groups(), 7, frozenset({"T1"}))
    assert chrome.ws.sent == []  # never guesses which tab is the agent's
    messages = _warnings(loguru_records)
    assert len(messages) == 1
    assert expected in messages[0] and "agent 7" in messages[0]


def _evaluate_throws(params: dict[str, Any]) -> dict[str, Any]:
    if params["expression"] == "typeof meivaGroup":
        return {"result": {"type": "string", "value": "function"}}
    return {
        "result": {"type": "object"},
        "exceptionDetails": {"exception": {"description": "Error: no tab for target T2"}},
    }


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        pytest.param(
            {"Extensions.loadUnpacked": RuntimeError("load refused")},
            "load refused",
            id="extension-load-fails",
        ),
        pytest.param(
            {"Target.getTargets": {"targetInfos": []}},
            "service worker target not found",
            id="worker-target-missing",
        ),
        pytest.param(
            {"Runtime.evaluate": _evaluate_throws},
            "no tab for target T2",
            id="worker-throws",
        ),
        pytest.param(
            {"Target.attachToTarget": RuntimeError("attach denied")},
            "attach denied",
            id="attach-fails",
        ),
    ],
)
async def test_cdp_failure_warns_with_cause_and_never_raises(
    override: dict[str, Any],
    needle: str,
    loguru_records: list[dict[str, Any]],
) -> None:
    handlers = {**_chrome_handlers(), **override}
    chrome = await FakeChrome.start(handlers)
    chrome.listings.append([_page("T1"), _page("T2")])

    await _group(chrome.groups(worker_wait_s=0.0), 7, frozenset({"T1"}))  # must not raise

    messages = _warnings(loguru_records)
    assert len(messages) == 1
    assert needle in messages[0] and "Meiva #7" in messages[0]
    assert "still open" in messages[0]  # tells the reader the page itself is fine


async def test_chrome_unreachable_warns_and_never_raises(
    loguru_records: list[dict[str, Any]],
) -> None:
    chrome = await FakeChrome.start(_chrome_handlers())
    chrome.listings.append(OSError("connection refused"))
    await _group(chrome.groups(), 7, frozenset({"T1"}))
    assert len(_warnings(loguru_records)) == 1


async def test_a_wedged_chrome_is_bounded(
    loguru_records: list[dict[str, Any]],
) -> None:
    chrome = await FakeChrome.start({}, wedged_http=True)
    groups = chrome.groups(listing_timeout_s=0.05)
    assert await groups.snapshot() is None
    await _group(groups, 7, frozenset())
    assert len(_warnings(loguru_records)) == 2


def test_extension_dir_ships_inside_the_package() -> None:
    """Hatch ships every file under the `services` package; the extension must
    live there so a non-editable install can still `loadUnpacked` it."""
    assert EXTENSION_DIR.is_dir()
    services_root = Path(grouping.__file__).resolve().parents[3]
    assert services_root.name == "services"
    assert EXTENSION_DIR.resolve().is_relative_to(services_root)


# meiva:note
# Off the daemon's lock: the page creation never waits for the grouping


async def test_group_new_page_returns_before_the_grouping_runs(
    loguru_records: list[dict[str, Any]],
) -> None:
    """The caller holds the daemon's serial lock: it gets control back after the one
    target listing, and the grouping (load, attach, call) happens afterwards."""
    chrome = await FakeChrome.start(_chrome_handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    groups = chrome.groups()

    await groups.group_new_page(7, frozenset({"T1"}))

    assert chrome.ws.sent == []  # nothing but the listing has happened yet
    assert len(groups._tasks) == 1
    await asyncio.gather(*groups._tasks)
    assert chrome.ws.methods()[-1] == "Runtime.evaluate"
    assert groups._tasks == set()
    assert _warnings(loguru_records) == []


async def test_a_wedged_chrome_costs_the_caller_nothing(
    loguru_records: list[dict[str, Any]],
) -> None:
    chrome = await FakeChrome.start(_chrome_handlers(), wedged_ws=True)
    chrome.listings.append([_page("T1"), _page("T2")])
    groups = chrome.groups()

    await asyncio.wait_for(groups.group_new_page(7, frozenset({"T1"})), timeout=1.0)
    await asyncio.wait_for(
        chrome.ws_entered.wait(), timeout=1.0
    )  # grouping is stuck in the background

    groups.cancel_pending()
    await asyncio.gather(*groups._tasks, return_exceptions=True)
    assert groups._tasks == set()
    assert (
        _warnings(loguru_records) == []
    )  # a cancelled grouping is the daemon's doing, not a failure


async def test_a_grouping_that_outlives_its_budget_warns(
    loguru_records: list[dict[str, Any]],
) -> None:
    chrome = await FakeChrome.start(_chrome_handlers(), wedged_ws=True)
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(chrome.groups(group_timeout_s=0.05), 7, frozenset({"T1"}))
    assert len(_warnings(loguru_records)) == 1


async def test_cancelling_and_failing_groupings_leave_no_unretrieved_exception() -> None:
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        broken = {**_chrome_handlers(), "Extensions.getExtensions": RuntimeError("boom")}
        chrome = await FakeChrome.start(broken)
        chrome.listings.append([_page("T1"), _page("T2")])
        failing = chrome.groups()
        await _group(failing, 7, frozenset({"T1"}))  # fails inside the task, reported by it

        chrome.wedged_ws = True
        chrome.listings.append([_page("T1"), _page("T2")])
        cancelled = chrome.groups()
        await cancelled.group_new_page(8, frozenset({"T1"}))
        cancelled.cancel_pending()
        await asyncio.gather(*cancelled._tasks, return_exceptions=True)

        del failing, cancelled
        gc.collect()  # "Task exception was never retrieved" is reported when a task is collected
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        loop.set_exception_handler(None)


async def test_back_to_back_pages_of_one_agent_make_one_group() -> None:
    """Two groupings of one agent in flight at once would each find no group by title
    and each create it. The groupings run one at a time (their own lock)."""
    created: list[str] = []
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER)
    chrome = await FakeChrome.start(model.handlers(), racy_groups=created)
    chrome.listings.extend(
        [
            [_page("T1"), _page("T2")],
            [_page("T1"), _page("T2"), _page("T3")],
        ]
    )
    groups = chrome.groups()

    await groups.group_new_page(7, frozenset({"T1"}))
    await groups.group_new_page(7, frozenset({"T1", "T2"}))
    assert len(groups._tasks) == 2  # both in flight together
    await asyncio.gather(*groups._tasks)

    assert created == ["Meiva #7"]
