# meiva:note
# `AgentTabGroups.tab_state` and `close_target`: the reads and the close the page ledger acts on.


from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest

from base.clock import SYSTEM_TIMERS
from services.desktop.browser.tab_groups.grouping import AgentTabGroups, ExtensionWorker
from services.desktop.browser.tab_groups.tests.test_grouping import (
    SHIPPED_WORKER,
    ChromeModel,
    FakeChrome,
)

_READ_FAILURES = (RuntimeError, OSError, TimeoutError, ValueError, TypeError, httpx.HTTPError)


def _page(target_id: str, url: str) -> dict[str, str]:
    return {"id": target_id, "type": "page", "url": url}


# meiva:note
# A Chrome whose extension answers `meivaGroupedTargets` and `meivaUngroupLast`.
class GroupedModel(ChromeModel):
    def __init__(self, grouped: list[dict[str, Any]] | Exception) -> None:
        super().__init__(loaded=True, worker=SHIPPED_WORKER)
        self.grouped = grouped
        self.chrome: FakeChrome | None = None
        self.closes_seen_at_ungroup: list[int] = []

    def _evaluate(self, params: dict[str, Any]) -> dict[str, Any] | Exception:
        expression = params["expression"]
        if expression == "meivaGroupedTargets()":
            self.expressions.append(expression)
            if isinstance(self.grouped, Exception):
                return self.grouped
            return {"result": {"type": "object", "value": self.grouped}}
        if expression.startswith("meivaUngroupLast(") and self.chrome is not None:
            closes = [u for u in self.chrome.http_urls if "/json/close/" in u]
            self.closes_seen_at_ungroup.append(len(closes))
        return super()._evaluate(params)


@pytest.fixture
async def chrome_factory() -> AsyncIterator[Callable[[dict[str, Any]], Awaitable[FakeChrome]]]:
    started: list[FakeChrome] = []

    async def start(handlers: dict[str, Any]) -> FakeChrome:
        chrome = await FakeChrome.start(handlers)
        started.append(chrome)
        return chrome

    yield start
    for chrome in started:
        await chrome.close()


def _groups(chrome: FakeChrome, tasks: asyncio.TaskGroup) -> AgentTabGroups:
    return AgentTabGroups(
        chrome.port, tasks, worker=ExtensionWorker(chrome.port, timers=SYSTEM_TIMERS)
    )


async def test_tab_state_reads_page_targets_and_the_agent_each_grouped_tab_belongs_to(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = GroupedModel(
        [
            {"targetId": "T1", "title": "Meiva #7"},
            {"targetId": "T2", "title": "Meiva #405"},
            {"targetId": "T3", "title": "Work"},
            {"targetId": "T4", "title": "Meiva #07"},
            {"targetId": "T5", "title": None},
            {"targetId": "T6", "title": "Meiva #x"},
            # meiva:note
            # a target /json/list no longer lists
            {"targetId": "GONE", "title": "Meiva #7"},
        ]
    )
    chrome = await chrome_factory(model.handlers())
    chrome.listings.append(
        [
            _page("T1", "https://a/"),
            _page("T2", "https://b/"),
            _page("T3", "https://c/"),
            _page("T4", "https://d/"),
            _page("T5", "https://e/"),
            _page("T6", "https://f/"),
            _page("T7", "https://ungrouped/"),
            {"id": "SW", "type": "service_worker", "url": "chrome-extension://x/sw.js"},
        ]
    )

    async with asyncio.TaskGroup() as tasks:
        state = await _groups(chrome, tasks).tab_state()

    assert state.pages == {f"T{n}": url for n, url in zip(range(1, 8), _urls(), strict=True)}
    assert dict(state.agent_groups) == {"T1": 7, "T2": 405}


def _urls() -> list[str]:
    return [f"https://{c}/" for c in "abcdef"] + ["https://ungrouped/"]


@pytest.mark.parametrize(
    "grouped",
    [RuntimeError("worker exploded"), [{"targetId": 5, "title": "Meiva #7"}], {"not": "a list"}],
)
async def test_tab_state_raises_when_the_extension_cannot_be_read(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
    grouped: Any,
) -> None:
    chrome = await chrome_factory(GroupedModel(grouped).handlers())
    chrome.listings.append([_page("T1", "https://a/")])

    async with asyncio.TaskGroup() as tasks:
        with pytest.raises(_READ_FAILURES):
            await _groups(chrome, tasks).tab_state()


async def test_tab_state_raises_when_chrome_cannot_be_listed(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    chrome = await chrome_factory(GroupedModel([]).handlers())
    chrome.listings.append(OSError("down"))

    async with asyncio.TaskGroup() as tasks:
        with pytest.raises(_READ_FAILURES):
            await _groups(chrome, tasks).tab_state()


async def test_close_target_ungroups_the_last_tab_closes_by_target_id_and_sees_it_gone(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = GroupedModel([])
    chrome = await chrome_factory(model.handlers())
    model.chrome = chrome
    chrome.listings.extend(["Target is closing", [_page("T1", "https://a/")]])

    async with asyncio.TaskGroup() as tasks:
        assert await _groups(chrome, tasks).close_target(7, "T2", grouped=True) is True

    ungroup = [e for e in model.expressions if e.startswith("meivaUngroupLast(")]
    assert len(ungroup) == 1 and '"T2"' in ungroup[0] and "Meiva #7" in ungroup[0]
    # meiva:note
    # the ungroup landed before the close request
    assert model.closes_seen_at_ungroup == [0]
    assert f"http://127.0.0.1:{chrome.port}/json/close/T2" in chrome.http_urls


async def test_close_target_of_an_ungrouped_page_does_not_touch_the_extension(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    model = GroupedModel([])
    chrome = await chrome_factory(model.handlers())
    chrome.listings.extend(["Target is closing", []])

    async with asyncio.TaskGroup() as tasks:
        assert await _groups(chrome, tasks).close_target(7, "T2", grouped=False) is True

    assert model.expressions == []


async def test_close_target_reports_a_target_that_is_still_open_after_the_close(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    chrome = await chrome_factory(GroupedModel([]).handlers())
    chrome.listings.append("Target is closing")
    chrome.listings.extend([[_page("T2", "https://a/")]] * 50)

    async with asyncio.TaskGroup() as tasks:
        groups = AgentTabGroups(
            chrome.port,
            tasks,
            worker=ExtensionWorker(chrome.port, timers=SYSTEM_TIMERS),
            close_settle_s=0.3,
        )
        assert await groups.close_target(7, "T2", grouped=False) is False


async def test_close_target_quotes_the_target_id_into_the_url(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    chrome = await chrome_factory(GroupedModel([]).handlers())
    chrome.listings.extend(["Target is closing", []])

    async with asyncio.TaskGroup() as tasks:
        await _groups(chrome, tasks).close_target(7, "../json/version", grouped=False)

    assert any(u.endswith("/json/close/..%2Fjson%2Fversion") for u in chrome.http_urls)


async def test_close_target_closes_nothing_when_the_last_tab_cannot_be_ungrouped(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    handlers = GroupedModel([]).handlers()
    handlers["Extensions.getExtensions"] = RuntimeError("no extensions")
    chrome = await chrome_factory(handlers)

    async with asyncio.TaskGroup() as tasks:
        with pytest.raises(_READ_FAILURES):
            await _groups(chrome, tasks).close_target(7, "T2", grouped=True)

    assert not any("/json/close/" in u for u in chrome.http_urls)


async def test_close_target_raises_when_chrome_does_not_answer_the_close(
    chrome_factory: Callable[[dict[str, Any]], Awaitable[FakeChrome]],
) -> None:
    chrome = await chrome_factory(GroupedModel([]).handlers())
    chrome.listings.append(OSError("closed the connection"))

    async with asyncio.TaskGroup() as tasks:
        with pytest.raises(_READ_FAILURES):
            await _groups(chrome, tasks).close_target(7, "T2", grouped=False)
