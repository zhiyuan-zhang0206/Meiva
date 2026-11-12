# meiva:note
# A Chrome the daemon can read and close pages in, and an in-memory ledger store.
#
# ``FakeBrowser`` stands in for ``AgentTabGroups`` in a daemon test: it models
# Chrome's page targets and their tab groups (the part of the browser that
# outlives a browser-mcp daemon), and the fake upstream creates and drops its
# targets as the upstream's pages come and go. A daemon "restart" in a test is a
# new daemon over the same ``FakeBrowser`` and the same ``MemoryStore``.


from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from services.desktop.browser.tab_groups.grouping import agent_id_of_title, group_title
from services.desktop.browser.tab_groups.page_ledger import LedgerFile, PageRecord, TabState


# meiva:note
# The slice of the fake upstream a browser binds to.
class BindableUpstream(Protocol):
    pages: dict[int, str]
    next_id: int
    browser: FakeBrowser | None


# meiva:note
# The selection file of a test: the agents' saved selections kept in memory, saves counted.
class MemorySelection:
    def __init__(self, selected: dict[int, str] | None = None) -> None:
        self.selected = dict(selected or {})
        self.saves = 0

    def load(self) -> dict[int, str]:
        return dict(self.selected)

    def save(self, selected: dict[int, str]) -> None:
        self.selected = dict(selected)
        self.saves += 1


# meiva:note
# The ledger file of a test: records kept in memory, saves counted.
# No ledger yet (`load` is None) until a test gives it one or something saves.
class MemoryStore:
    def __init__(
        self, records: Mapping[str, PageRecord] | None = None, *, migrated: bool | None = None
    ) -> None:
        self.file: LedgerFile | None = (
            None
            if records is None and migrated is None
            else LedgerFile(dict(records or {}), bool(migrated))
        )
        self.saves = 0

    @property
    def records(self) -> dict[str, PageRecord]:
        return dict(self.file.records) if self.file is not None else {}

    def load(self) -> LedgerFile | None:
        return self.file

    def save(self, file: LedgerFile) -> None:
        self.file = LedgerFile(dict(file.records), file.migrated)
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
        self.grouping_fails = False
        self.stays_open: set[str] = set()
        self.ungroup_error: Exception | None = None
        self.on_before_close: Callable[[str], None] | None = None
        self.state_reads = 0
        self.on_state_read: Callable[[int], None] | None = None
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

    async def new_target(self, agent_id: int, before: frozenset[str] | None) -> str | None:
        if before is None:
            return None
        new = set(self.targets) - before
        return next(iter(new)) if len(new) == 1 else None

    async def group_new_page(
        self,
        agent_id: int,
        before: frozenset[str] | None,
        page_id: int | None = None,
        *,
        on_grouped: Callable[[str], None] | None = None,
    ) -> str | None:
        target_id = await self.new_target(agent_id, before)
        if target_id is None:
            return None
        if not self.grouping_fails:
            self.titles[target_id] = group_title(agent_id)
            if on_grouped is not None:
                on_grouped(target_id)
        return target_id

    async def release_page(self, page_id: int) -> None:
        return None

    async def converge_all(self) -> None:
        return None

    async def tab_state(self) -> TabState:
        self.state_reads += 1
        if self.on_state_read is not None:
            self.on_state_read(self.state_reads)
        if self.read_error is not None:
            raise self.read_error
        agent_groups = {
            target_id: agent_id
            for target_id, title in self.titles.items()
            if target_id in self.targets and (agent_id := agent_id_of_title(title)) is not None
        }
        other = frozenset(t for t in self.titles if t in self.targets and t not in agent_groups)
        return TabState(pages=dict(self.targets), agent_groups=agent_groups, other_groups=other)

    async def close_target(self, agent_id: int, target_id: str, *, grouped: bool) -> bool:
        if self.on_before_close is not None:
            self.on_before_close(target_id)
        if grouped and self.ungroup_error is not None:
            raise self.ungroup_error
        if self.close_error is not None:
            raise self.close_error
        if target_id not in self.targets:
            raise RuntimeError(f"no target {target_id}")
        if target_id in self.stays_open:
            return False
        self.closed.append(target_id)
        self._forget(target_id)
        return True
