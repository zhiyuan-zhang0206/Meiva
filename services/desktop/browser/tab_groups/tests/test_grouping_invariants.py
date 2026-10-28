# meiva:note
# The tab-group invariant, and the one write that can destroy a group.
#
# The JS half decides all of it (there is no Python model of Chrome's groups that
# could drift); these pin the policy the worker must keep, the reports it must send,
# the WARNING the daemon must leave when it had to merge a duplicate, and the Python
# side of ungrouping an agent's last tab. Behaviour against a real Chrome (two groups
# in one window, two windows, a second no-op run) is recorded in
# services/desktop/browser/docs/tab-groups.meiva.okf.md.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from base.clock import SYSTEM_TIMERS
from services.desktop.browser.tab_groups.grouping import EXTENSION_DIR, AgentTabGroups
from services.desktop.browser.tab_groups.tests.test_grouping import (
    SHIPPED_WORKER,
    ChromeModel,
    FakeChrome,
)


def _page(target_id: str, url: str = "https://example.com/") -> dict[str, str]:
    return {"id": target_id, "type": "page", "url": url}


def _warnings(records: list[dict[str, Any]]) -> list[str]:
    return [r["message"] for r in records if r["level"].name == "WARNING"]


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
# The policy the shipped worker must keep, in source form: a window-scoped lookup,
# a keeper chosen from the state rather than from Chrome's answer order, the same
# convergence on Chrome start and extension update, and no touch of a group this
# stack did not name.
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
def test_worker_converges_one_group_per_agent_per_window() -> None:
    source = next(EXTENSION_DIR.glob("group-*.js")).read_text()
    assert "chrome.tabGroups.query({ title, windowId })" in source
    assert "query({ title })" not in source
    assert "b.size - a.size || a.id - b.id" in source
    assert "meivaConvergeWindow(title, tab.windowId)" in source
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
        groups = AgentTabGroups(chrome.port, tasks, timers=SYSTEM_TIMERS)
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
        groups = AgentTabGroups(chrome.port, tasks, timers=SYSTEM_TIMERS)
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
        groups = AgentTabGroups(chrome.port, tasks, timers=SYSTEM_TIMERS)
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
        groups = AgentTabGroups(chrome.port, tasks, timers=SYSTEM_TIMERS)
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
        groups = AgentTabGroups(chrome.port, tasks, timers=SYSTEM_TIMERS)
        await groups.group_new_page(7, frozenset({"T1"}))
        await _settle(7)

    messages = _warnings(loguru_records)
    assert len(messages) == 1 and "could not file agent 7" in messages[0], messages


# meiva:note
# release_page, ungroup before the last tab closes: Chrome keeps a group whose last
# tab closes as a SAVED group, and the extension API neither lists nor deletes one.
# The daemon therefore ungroups that tab first; these pin the Python half of that
# contract (the JS half decides that the tab is really last and really in this
# agent's group).
# meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md


async def test_release_page_ungroups_the_recorded_target_of_the_agent(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER)
    chrome = await chrome_factory(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])

    async with asyncio.TaskGroup() as tasks:
        groups = AgentTabGroups(chrome.port, tasks, timers=SYSTEM_TIMERS)
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
        await AgentTabGroups(chrome.port, tasks, timers=SYSTEM_TIMERS).release_page(99)

    assert chrome.http_urls == []
    assert model.expressions == []


async def test_release_page_does_not_start_a_worker_and_only_warns(
    loguru_records: list[dict[str, Any]],
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    # meiva:note
    # A worker that is not running cannot have left a saved group, so the ungroup
    # never loads one -- it warns that the chip may be left behind.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    model = ChromeModel(loaded=False)
    chrome = await chrome_factory(model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    async with asyncio.TaskGroup() as tasks:
        groups = AgentTabGroups(chrome.port, tasks, timers=SYSTEM_TIMERS)
        await groups.group_new_page(42, frozenset({"T1"}), 7)
        await _settle(42)
        model.expressions.clear()
        loaded_before = chrome.ws.methods().count("Extensions.loadUnpacked")
        model.worker = None
        await groups.release_page(7)

    assert chrome.ws.methods().count("Extensions.loadUnpacked") == loaded_before
    messages = _warnings(loguru_records)
    assert any("saved tab group" in m for m in messages), messages
