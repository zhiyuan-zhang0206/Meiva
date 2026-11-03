# meiva:note
# The tab-group invariant, and the one write that can destroy a group.
#
# The JS half decides all of it (there is no Python model of Chrome's groups that
# could drift); these pin the policy the worker must keep, the reports it must send,
# the WARNING the daemon must leave when it had to merge a duplicate, and the Python
# side of ungrouping an agent's last tab (including waking an idled-out service
# worker before closing the page).
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from base.clock import SYSTEM_TIMERS
from services.desktop.browser.tab_groups.grouping import (
    EXTENSION_DIR,
    AgentTabGroups,
    ExtensionWorker,
)
from services.desktop.browser.tab_groups.tests.test_grouping import (
    SHIPPED_WORKER,
    ChromeModel,
    FakeChrome,
)


def _page(target_id: str, url: str = "https://example.com/") -> dict[str, str]:
    return {"id": target_id, "type": "page", "url": url}


def _warnings(records: list[dict[str, Any]]) -> list[str]:
    return [r["message"] for r in records if r["level"].name == "WARNING"]


def _groups(chrome: FakeChrome, tasks: asyncio.TaskGroup) -> AgentTabGroups:
    return AgentTabGroups(
        chrome.port,
        tasks,
        worker=ExtensionWorker(chrome.port, timers=SYSTEM_TIMERS),
    )


# meiva:note
# The daemon names its background grouping tasks, so a test can wait for the one it
# started without reaching into the facade's bookkeeping.
async def _settle(agent_id: int) -> None:
    pending = [t for t in asyncio.all_tasks() if t.get_name() == f"tab-group-agent-{agent_id}"]
    await asyncio.gather(*pending)


@pytest.fixture
async def chrome_factory() -> AsyncIterator[Callable[[dict[str, Any]], Awaitable[FakeChrome]]]:
    """Start fake Chromes and close every one of them when the test ends."""
    started: list[FakeChrome] = []

    async def start(handlers: dict[str, Any]) -> FakeChrome:
        chrome = await FakeChrome.start(handlers)
        started.append(chrome)
        return chrome

    yield start
    for chrome in started:
        await chrome.close()


# meiva:note
# The policy the shipped worker must keep, in source form: a browser-wide lookup
# for `Meiva #<digits>` titles, a keeper chosen deterministically from Chrome's
# state rather than answer order, keeping the keeper anchored in its window, the
# same convergence on Chrome start and extension update, and no touch of a group
# this stack did not name.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
def test_worker_converges_one_group_per_agent_across_the_browser() -> None:
    source = next(EXTENSION_DIR.glob("group-*.js")).read_text()
    assert "chrome.tabGroups.query({ title })" in source
    assert "query({ title, windowId })" not in source
    assert "b.size - a.size || a.id - b.id" in source
    assert "meivaConvergeTitle(title)" in source
    assert "meivaGroupIntoKeeper" in source
    assert "self.meivaConvergeAll" in source
    assert "chrome.runtime.onStartup.addListener" in source
    assert "chrome.runtime.onInstalled.addListener" in source
    assert "MEIVA_TITLE.test(owner.title)" in source


async def test_a_merged_duplicate_is_reported_with_agent_window_and_group_ids(
    loguru_records: list[dict[str, Any]],
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = ChromeModel(
        loaded=True,
        worker=SHIPPED_WORKER,
        report={
            "title": "Meiva #7",
            "windowId": 737373,
            "groups": [515151, 626262],
            "kept": 515151,
            "merged": 1,
            "moved": 2,
        },
    )
    chrome = await chrome_factory(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])

    async with asyncio.TaskGroup() as tasks:
        groups = _groups(chrome, tasks)
        await groups.group_new_page(7, frozenset({"T1"}))
        await _settle(7)

    messages = _warnings(loguru_records)
    assert len(messages) == 1, messages
    for needle in ("agent 7", "737373", "515151", "626262", "Meiva #7"):
        assert needle in messages[0], messages[0]


async def test_a_single_group_is_the_normal_path_and_says_nothing(
    loguru_records: list[dict[str, Any]],
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER)
    chrome = await chrome_factory(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])

    async with asyncio.TaskGroup() as tasks:
        groups = _groups(chrome, tasks)
        await groups.group_new_page(7, frozenset({"T1"}))
        await _settle(7)
        await groups.converge_all()

    assert _warnings(loguru_records) == []


async def test_converge_all_reports_every_merged_duplicate_of_every_agent(
    loguru_records: list[dict[str, Any]],
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = ChromeModel(
        loaded=True,
        worker=SHIPPED_WORKER,
        converge=[
            {
                "title": "Meiva #7",
                "windowId": 737373,
                "groups": [515151, 626262],
                "kept": 515151,
                "merged": 1,
                "moved": 3,
            },
            {
                "title": "Meiva #8",
                "windowId": 747474,
                "groups": [717171],
                "kept": 717171,
                "merged": 0,
                "moved": 0,
            },
        ],
    )
    chrome = await chrome_factory(model.handlers())

    async with asyncio.TaskGroup() as tasks:
        groups = _groups(chrome, tasks)
        await groups.converge_all()

    messages = _warnings(loguru_records)
    assert len(messages) == 1, messages
    for needle in ("Meiva #7", "737373", "515151", "626262"):
        assert needle in messages[0], messages[0]


async def test_converge_all_failure_warns_and_never_raises(
    loguru_records: list[dict[str, Any]],
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = ChromeModel(
        loaded=True, worker=SHIPPED_WORKER, converge_error=RuntimeError("Runtime.evaluate: boom")
    )
    chrome = await chrome_factory(model.handlers())

    async with asyncio.TaskGroup() as tasks:
        groups = _groups(chrome, tasks)
        await groups.converge_all()

    assert model.expressions.count("meivaConvergeAll()") == 1
    messages = _warnings(loguru_records)
    assert len(messages) == 1 and "converge" in messages[0], messages


async def test_a_malformed_report_warns_and_keeps_the_page(
    loguru_records: list[dict[str, Any]],
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = ChromeModel(
        loaded=True,
        worker=SHIPPED_WORKER,
        report={"title": "Meiva #7", "windowId": "not a window", "groups": [1], "kept": 1},
    )
    chrome = await chrome_factory(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])

    async with asyncio.TaskGroup() as tasks:
        groups = _groups(chrome, tasks)
        await groups.group_new_page(7, frozenset({"T1"}))
        await _settle(7)

    messages = _warnings(loguru_records)
    assert len(messages) == 1 and "could not file agent 7" in messages[0], messages


# meiva:note
# release_page, ungroup before the last tab closes: Chrome keeps a group whose last
# tab closes as a SAVED group, and the extension API neither lists nor deletes one.
# The daemon therefore ungroups that tab first; these pin the Python half of that
# contract (including waking an idled-out MV3 service worker before closing).
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
async def test_release_page_ungroups_the_recorded_target_of_the_agent(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER)
    chrome = await chrome_factory(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])

    async with asyncio.TaskGroup() as tasks:
        groups = _groups(chrome, tasks)
        await groups.group_new_page(42, frozenset({"T1"}), 7)
        await _settle(42)
        await groups.release_page(7)

    expressions = [e for e in model.expressions if e.startswith("meivaUngroupLast(")]
    assert len(expressions) == 1
    assert '"T2"' in expressions[0]
    assert "Meiva #42" in expressions[0]


async def test_release_page_is_a_no_op_for_a_page_it_never_grouped(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER)
    chrome = await chrome_factory(model.handlers())

    async with asyncio.TaskGroup() as tasks:
        await _groups(chrome, tasks).release_page(99)

    assert chrome.http_urls == []
    assert model.expressions == []


async def test_release_page_wakes_an_idled_out_worker_to_ungroup_the_last_tab(
    loguru_records: list[dict[str, Any]],
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    # meiva:note
    # Chrome stops the MV3 service worker ~30s after grouping finishes while the
    # open tab group remains in the tab strip; `release_page` must reload/wake the
    # worker via `loadUnpacked` so closing an agent's tab after >30s removes the
    # group instead of saving a closed group chip.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    model = ChromeModel(loaded=False)
    chrome = await chrome_factory(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    async with asyncio.TaskGroup() as tasks:
        groups = _groups(chrome, tasks)
        await groups.group_new_page(42, frozenset({"T1"}), 7)
        await _settle(42)
        model.expressions.clear()
        loaded_before = chrome.ws.methods().count("Extensions.loadUnpacked")
        model.worker = None
        await groups.release_page(7)

    assert chrome.ws.methods().count("Extensions.loadUnpacked") == loaded_before + 1
    expressions = [e for e in model.expressions if e.startswith("meivaUngroupLast(")]
    assert expressions == ['meivaUngroupLast("T2", "Meiva #42")']
    assert _warnings(loguru_records) == []


async def test_release_page_serializes_behind_in_flight_grouping(
    loguru_records: list[dict[str, Any]],
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    # meiva:note
    # A page closed immediately after creation shares `ExtensionWorker`'s lock with
    # its background `meivaGroup` task, so grouping cannot finish after ungroup and
    # leave a grouped tab to close.
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER, script_delay=2)
    chrome = await chrome_factory(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    async with asyncio.TaskGroup() as tasks:
        groups = _groups(chrome, tasks)
        await groups.group_new_page(42, frozenset({"T1"}), 7)
        await asyncio.sleep(0)
        await groups.release_page(7)
        await _settle(42)

    calls = [e for e in model.expressions if e.startswith(("meivaGroup(", "meivaUngroupLast("))]
    assert calls == [
        'meivaGroup("T2", "Meiva #42")',
        'meivaUngroupLast("T2", "Meiva #42")',
    ]
    assert _warnings(loguru_records) == []
