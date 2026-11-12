# meiva:note
# A Chrome the daemon can read and close pages in, and an in-memory ledger store.
#
# ``FakeBrowser`` stands in for ``AgentTabGroups`` in a daemon test: it models
# Chrome's page targets and their tab groups (the part of the browser that
# outlives a browser-mcp daemon), and the fake upstream creates and drops its
# targets as the upstream's pages come and go. A daemon "restart" in a test is a
# new daemon over the same ``FakeBrowser`` and the same ``MemoryStore``.


from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from services.desktop.browser.tab_groups.grouping import agent_id_of_title, group_title
from services.desktop.browser.tab_groups.page_ledger import PageRecord, TabState


# meiva:note
# The slice of the fake upstream a browser binds to.
class BindableUpstream(Protocol):
    pages: dict[int, str]
    next_id: int
    browser: FakeBrowser | None


# meiva:note
# The ledger file of a test: records kept in memory, saves counted.
class MemoryStore:
    def __init__(self, records: Mapping[str, PageRecord] | None = None) -> None:
        self.records: dict[str, PageRecord] = dict(records or {})
        self.saves = 0

    def load(self) -> dict[str, PageRecord]:
        return dict(self.records)

    def save(self, records: Mapping[str, PageRecord]) -> None:
        self.records = dict(records)
        self.saves += 1


# meiva:note
# Chrome's page targets, their tab-group titles, and what the daemon did to them.
class FakeBrowser:
    def __init__(self, *, blank_tab: bool = True) -> None:
        self.targets: dict[str, str] = {}
        self.titles: dict[str, str] = {}
        self.closed: list[str] = []
        self.read_error: Exception | None = None
        self.close_error: Exception | None = None
        self._upstream_pages: dict[int, str] | None = None
        self._page_target: dict[int, str] = {}
        self._count = 0
        if blank_tab:
            # meiva:note
            # a browser always has a tab the user never made
            self.add_tab("about:blank")

    # meiva:note
    # Let ``upstream`` (a FakeUpstream) create and drop this browser's targets.
    def bind(self, upstream: BindableUpstream) -> None:
        self._upstream_pages = upstream.pages
        upstream.browser = self

    # meiva:note
    # A tab Chrome already has: the user's (no group), or one in group ``title``.
    def add_tab(self, url: str, *, title: str | None = None) -> str:
        self._count += 1
        target_id = f"T{self._count}"
        self.targets[target_id] = url
        if title is not None:
            self.titles[target_id] = title
        return target_id

    # meiva:note
    # A new upstream process over the same Chrome: it numbers every open page afresh.
    def rebind(self, upstream: BindableUpstream) -> None:
        self._page_target = {}
        self._upstream_pages = upstream.pages
        upstream.browser = self
        upstream.pages.clear()
        for number, (target_id, url) in enumerate(self.targets.items(), start=1):
            upstream.pages[number] = url
            self._page_target[number] = target_id
        upstream.next_id = len(self.targets) + 1

    # meiva:note
    # Chrome restarted and restored its tabs and groups: same URLs and titles, new targets.
    def restart_chrome(self) -> None:
        old = list(self.targets.items())
        titles = dict(self.titles)
        self.targets.clear()
        self.titles.clear()
        self._page_target.clear()
        for target_id, url in old:
            self.add_tab(url, title=titles.get(target_id))

    # meiva:note
    # The upstream page number showing ``url`` (it must be unique).
    def page_id_of(self, url: str) -> int:
        matches = [n for n, t in self._page_target.items() if self.targets[t] == url]
        assert len(matches) == 1, matches
        return matches[0]

    def upstream_page_created(self, page_id: int, url: str) -> None:
        self._page_target[page_id] = self.add_tab(url)

    def upstream_page_closed(self, page_id: int) -> None:
        target_id = self._page_target.pop(page_id, None)
        if target_id is not None:
            self._forget(target_id)

    def target_of_upstream_page(self, page_id: int) -> str:
        return self._page_target[page_id]

    def _forget(self, target_id: str) -> None:
        self.targets.pop(target_id, None)
        self.titles.pop(target_id, None)
        for page_id, known in list(self._page_target.items()):
            if known == target_id:
                del self._page_target[page_id]
                if self._upstream_pages is not None:
                    self._upstream_pages.pop(page_id, None)

    async def snapshot(self) -> frozenset[str] | None:
        return frozenset(self.targets)

    async def group_new_page(
        self, agent_id: int, before: frozenset[str] | None, page_id: int | None = None
    ) -> str | None:
        if before is None:
            return None
        new = set(self.targets) - before
        if len(new) != 1:
            return None
        target_id = next(iter(new))
        self.titles[target_id] = group_title(agent_id)
        return target_id

    async def release_page(self, page_id: int) -> None:
        return None

    async def converge_all(self) -> None:
        return None

    async def tab_state(self) -> TabState:
        if self.read_error is not None:
            raise self.read_error
        agent_groups = {
            target_id: agent_id
            for target_id, title in self.titles.items()
            if target_id in self.targets and (agent_id := agent_id_of_title(title)) is not None
        }
        return TabState(pages=dict(self.targets), agent_groups=agent_groups)

    async def close_target(self, agent_id: int, target_id: str) -> None:
        if self.close_error is not None:
            raise self.close_error
        if target_id not in self.targets:
            raise RuntimeError(f"no target {target_id}")
        self.closed.append(target_id)
        self._forget(target_id)
