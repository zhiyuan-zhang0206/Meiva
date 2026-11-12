# meiva:note
# The page ledger across browser-mcp daemon restarts, Chrome restarts and corrupt state.
#
# A "daemon restart" here is a new ``ChromeMcpDaemon`` over the same ``FakeBrowser`` (Chrome
# keeps running, its page targets and tab groups stay) and the same ``MemoryStore`` (the
# ledger file), with a new fake upstream that numbers the open pages afresh -- exactly what a
# cluster update or an upstream ``chrome-devtools-mcp`` death does. The invariant every test
# here holds: a page outside a ``Meiva #<id>`` tab group is never closed.


from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from services.desktop.browser import page_lifecycle
from services.desktop.browser.mcp_daemon import ChromeMcpDaemon, text_of
from services.desktop.browser.page_lifecycle import FileRecordStore, PageRegistry
from services.desktop.browser.tab_groups.page_ledger import PageRecord
from services.desktop.browser.tests.fake_browser import (
    FakeBrowser,
    MemoryStore,
)
from services.desktop.browser.tests.test_browser_mcp_daemon import FakeUpstream, new_daemon

TTL = 3_600.0


# meiva:note
# A wall clock a test sets: it can step forwards and backwards.
class Wall:
    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)

    def clock(self) -> Clock:
        return Clock(
            ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=lambda: self.moment
        )


# meiva:note
# One Chrome, one ledger file and one wall clock, outliving every daemon started on them.
@dataclass
class Rig:
    browser: FakeBrowser = field(default_factory=FakeBrowser)
    store: MemoryStore = field(default_factory=MemoryStore)
    wall: Wall = field(default_factory=Wall)

    def start(self) -> tuple[ChromeMcpDaemon, FakeUpstream]:
        up = FakeUpstream()
        self.browser.rebind(up)
        daemon = new_daemon(
            up,
            tab_groups=self.browser,
            default_ttl_seconds=TTL,
            store=self.store,
            clock=self.wall.clock(),
        )
        return daemon, up


def _daemon_on_file(rig: Rig, store: FileRecordStore) -> ChromeMcpDaemon:
    up = FakeUpstream()
    rig.browser.rebind(up)
    return new_daemon(
        up, tab_groups=rig.browser, default_ttl_seconds=TTL, store=store, clock=rig.wall.clock()
    )


async def _open(d: ChromeMcpDaemon, agent_id: int, url: str) -> None:
    result = await d.call_tool_for_agent("new_page", {"url": url}, agent_id)
    assert not result.is_error


def _open_urls(rig: Rig) -> set[str]:
    return set(rig.browser.targets.values())


def _infos(records: list[dict[str, Any]]) -> list[str]:
    return [r["message"] for r in records if r["level"].name == "INFO"]


def _warnings(records: list[dict[str, Any]]) -> list[str]:
    return [r["message"] for r in records if r["level"].name == "WARNING"]


# meiva:note
# ── The record survives a daemon restart ────────────────────────────────────


async def test_a_page_created_before_a_daemon_restart_is_swept_after_its_deadline() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await _open(d1, 7, "https://a.example/")
    assert len(rig.store.records) == 1

    # meiva:note
    # the daemon restarted; Chrome and the ledger file did not
    d2, up2 = rig.start()
    # meiva:note
    # the upstream's page numbers meant nothing to it
    assert d2.pages.page_targets == {}
    rig.wall.advance(TTL - 1)
    assert await page_lifecycle.reap_expired_pages(d2) == 0
    rig.wall.advance(2)
    assert await page_lifecycle.reap_expired_pages(d2) == 1

    assert _open_urls(rig) == {"about:blank"}
    assert up2.pages == {1: "about:blank"}
    assert rig.store.records == {}


async def test_release_after_a_daemon_restart_closes_every_page_of_that_agent_only() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await _open(d1, 7, "https://a1.example/")
    await _open(d1, 7, "https://a2.example/")
    await _open(d1, 8, "https://b.example/")
    user_tab = rig.browser.add_tab("https://user.example/")

    d2, _ = rig.start()
    # meiva:note
    # no page ids to report
    assert await page_lifecycle.release_agent_page(d2, 7) is None
    assert d2.pages.get_agent_page(7) is None

    assert _open_urls(rig) == {"about:blank", "https://b.example/", "https://user.example/"}
    assert user_tab in rig.browser.targets
    assert [r.owner for r in rig.store.records.values()] == [8]


async def test_renew_after_a_daemon_restart_moves_the_recorded_deadline() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await _open(d1, 7, "https://a.example/")

    d2, _ = rig.start()
    d2.pages.set_agent_page(7, rig.browser.page_id_of("https://a.example/"))
    result = await d2.call_tool_for_agent("renew_page", {"ttl": 2 * TTL}, 7)
    assert not result.is_error, text_of(result)

    rig.wall.advance(TTL + 1)
    assert await page_lifecycle.reap_expired_pages(d2) == 0
    rig.wall.advance(TTL + 1)
    assert await page_lifecycle.reap_expired_pages(d2) == 1


async def test_renew_after_a_restart_refuses_an_ambiguous_page_instead_of_guessing() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await _open(d1, 7, "https://same.example/")
    await _open(d1, 7, "https://same.example/")

    d2, up2 = rig.start()
    d2.pages.set_agent_page(7, 2)
    result = await d2.call_tool_for_agent("renew_page", {"ttl": 60}, 7)
    assert result.is_error
    assert "no page TTL" in text_of(result)
    assert up2.pages[2] == "https://same.example/"


async def test_renew_after_a_restart_refuses_a_page_the_agent_does_not_own() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await _open(d1, 8, "https://b.example/")
    rig.browser.add_tab("https://user.example/")

    d2, _ = rig.start()
    for url in ("https://b.example/", "https://user.example/"):
        d2.pages.set_agent_page(7, rig.browser.page_id_of(url))
        result = await d2.call_tool_for_agent("renew_page", {"ttl": 60}, 7)
        assert result.is_error, url


async def test_a_record_of_a_page_chrome_no_longer_has_is_dropped() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await _open(d1, 7, "https://a.example/")
    (target,) = rig.store.records
    # meiva:note
    # the user closed the tab by hand while the daemon was down
    rig.browser.targets.pop(target)

    d2, _ = rig.start()
    assert await page_lifecycle.reap_expired_pages(d2) == 0
    assert rig.store.records == {}


# meiva:note
# ── Adoption: a page in an agent group with no record ───────────────────────


async def test_a_grouped_page_without_a_record_is_adopted_and_closed_one_ttl_later(
    loguru_records: list[dict[str, Any]],
) -> None:
    rig = Rig()
    rig.browser.add_tab("https://old.example/", title="Meiva #9")
    rig.browser.add_tab("https://old2.example/", title="Meiva #9")
    rig.browser.add_tab("https://old3.example/", title="Meiva #4")
    d, _ = rig.start()

    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert sorted(r.owner for r in rig.store.records.values()) == [4, 9, 9]
    assert {r.expires_at for r in rig.store.records.values()} == {d.pages.now() + TTL}
    adoption = [m for m in _infos(loguru_records) if "adopted" in m]
    assert len(adoption) == 1
    assert (
        "3 page(s)" in adoption[0] and "agent 4: 1" in adoption[0] and "agent 9: 2" in adoption[0]
    )

    rig.wall.advance(TTL + 1)
    assert await page_lifecycle.reap_expired_pages(d) == 3
    assert _open_urls(rig) == {"about:blank"}


async def test_chrome_restart_new_targets_in_agent_groups_are_adopted_not_leaked() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await _open(d1, 7, "https://a.example/")
    # meiva:note
    # restored tabs and groups, new target ids
    rig.browser.restart_chrome()

    d2, _ = rig.start()
    assert await page_lifecycle.reap_expired_pages(d2) == 0
    (record,) = rig.store.records.values()
    assert record.owner == 7
    rig.wall.advance(TTL + 1)
    assert await page_lifecycle.reap_expired_pages(d2) == 1
    assert _open_urls(rig) == {"about:blank"}


async def test_an_invalid_group_title_never_adopts() -> None:
    rig = Rig()
    for title in ("Meiva #x", "Meiva #07", "Meiva #7 ", "meiva #7", "Meiva #", "Work", "Meiva #-1"):
        rig.browser.add_tab(f"https://{abs(hash(title))}.example/", title=title)
    d, _ = rig.start()
    rig.wall.advance(100 * TTL)

    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert rig.store.records == {}
    assert rig.browser.closed == []


# meiva:note
# ── The floor: a page outside a Meiva group is never closed ────────────────


async def test_pages_outside_meiva_groups_are_never_closed_in_any_path() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await _open(d1, 7, "https://mine.example/")
    plain = rig.browser.add_tab("https://plain.example/")
    in_work = rig.browser.add_tab("https://work.example/", title="Work")
    bad_title = rig.browser.add_tab("https://bad.example/", title="Meiva #07")
    moved_out = rig.browser.add_tab("https://moved.example/", title="Meiva #7")
    # meiva:note
    # A stale or hostile ledger naming the user's tabs, already expired: a record alone never
    # proves a page ours.
    rig.store.records.update(
        {t: PageRecord(7, 0.0) for t in (plain, in_work, bad_title, moved_out)}
    )
    # meiva:note
    # the user moved it out of the agent's group
    rig.browser.titles[moved_out] = "Work"

    d2, _ = rig.start()
    rig.wall.advance(10 * TTL)
    for _ in range(2):
        await page_lifecycle.reap_expired_pages(d2)
        await page_lifecycle.release_agent_page(d2, 7)
        await page_lifecycle.release_agent_page(d2, 9)

    survivors = set(rig.browser.targets)
    assert {plain, in_work, bad_title, moved_out} <= survivors
    # meiva:note
    # only the grouped, recorded page went
    assert "https://mine.example/" not in _open_urls(rig)
    assert rig.store.records == {}


async def test_an_expired_record_whose_page_left_the_group_is_dropped_with_a_warning(
    loguru_records: list[dict[str, Any]],
) -> None:
    rig = Rig()
    d1, _ = rig.start()
    await _open(d1, 7, "https://a.example/")
    (target,) = rig.store.records
    rig.browser.titles[target] = "Work"

    d2, _ = rig.start()
    rig.wall.advance(TTL + 1)
    assert await page_lifecycle.reap_expired_pages(d2) == 0

    assert target in rig.browser.targets
    assert rig.store.records == {}
    assert any("in no agent tab group" in m for m in _warnings(loguru_records))


async def test_an_unreadable_chrome_closes_nothing_and_keeps_the_records(
    loguru_records: list[dict[str, Any]],
) -> None:
    rig = Rig()
    d, _ = rig.start()
    await _open(d, 7, "https://a.example/")
    rig.wall.advance(TTL + 1)
    rig.browser.read_error = TimeoutError("extension wedged")

    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert await page_lifecycle.release_agent_page(d, 7) is None

    assert rig.browser.closed == []
    assert len(rig.store.records) == 1
    assert any("could not read Chrome's tabs" in m for m in _warnings(loguru_records))
    rig.browser.read_error = None
    assert await page_lifecycle.reap_expired_pages(d) == 1


async def test_a_close_chrome_refuses_keeps_the_record_and_is_retried(
    loguru_records: list[dict[str, Any]],
) -> None:
    rig = Rig()
    d, _ = rig.start()
    await _open(d, 7, "https://a.example/")
    rig.wall.advance(TTL + 1)
    rig.browser.close_error = RuntimeError("Chrome said no")

    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert len(rig.store.records) == 1
    assert any("could not close page" in m for m in _warnings(loguru_records))

    rig.browser.close_error = None
    assert await page_lifecycle.reap_expired_pages(d) == 1
    assert rig.store.records == {}


async def test_the_last_page_of_the_browser_is_never_closed() -> None:
    rig = Rig()
    rig.browser = FakeBrowser(blank_tab=False)
    d, _ = rig.start()
    await _open(d, 7, "https://only.example/")
    rig.wall.advance(TTL + 1)

    assert await page_lifecycle.reap_expired_pages(d) == 0
    assert await page_lifecycle.release_agent_page(d, 7) is None
    assert _open_urls(rig) == {"https://only.example/"}

    rig.browser.add_tab("https://another.example/")
    assert await page_lifecycle.reap_expired_pages(d) == 1


# meiva:note
# ── Wall-clock steps ────────────────────────────────────────────────────────


async def test_a_clock_stepped_backwards_never_closes_a_page_early_and_bounds_the_wait() -> None:
    rig = Rig()
    d, _ = rig.start()
    await _open(d, 7, "https://a.example/")
    # meiva:note
    # the clock is corrected 10 days back
    rig.wall.advance(-10 * 86_400)

    assert await page_lifecycle.reap_expired_pages(d) == 0
    (record,) = rig.store.records.values()
    # meiva:note
    # the deadline is pulled back to one grant (24h, the larger of the grant cap and the TTL)
    assert record.expires_at == d.pages.now() + 86_400.0

    rig.wall.advance(86_399)
    assert await page_lifecycle.reap_expired_pages(d) == 0
    rig.wall.advance(2)
    assert await page_lifecycle.reap_expired_pages(d) == 1


# meiva:note
# ── The ledger file ─────────────────────────────────────────────────────────


async def test_the_ledger_file_is_written_on_create_renew_and_close(tmp_path: Path) -> None:
    path = tmp_path / "run" / "browser-pages.json"
    rig = Rig()
    registry_store = FileRecordStore(path)
    d = _daemon_on_file(rig, registry_store)
    await _open(d, 7, "https://a.example/")
    assert path.exists() and oct(path.stat().st_mode & 0o777) == "0o600"
    assert len(FileRecordStore(path).load()) == 1

    await d.call_tool_for_agent("renew_page", {"ttl": 100}, 7)
    (record,) = FileRecordStore(path).load().values()
    assert record.expires_at == rig.wall.moment.timestamp() + 100

    await d.call_tool_for_agent("close_page", {"pageId": 2}, 7)
    assert FileRecordStore(path).load() == {}


async def test_a_corrupt_ledger_file_reads_as_empty_and_the_pages_are_adopted(
    tmp_path: Path, loguru_records: list[dict[str, Any]]
) -> None:
    path = tmp_path / "browser-pages.json"
    rig = Rig()
    rig.browser.add_tab("https://old.example/", title="Meiva #9")
    for garbage in ("{not json", "[]", '{"version": 2, "pages": {}}', "", '{"version": 1}'):
        path.write_text(garbage, encoding="utf-8")
        loguru_records.clear()
        registry = PageRegistry(
            default_ttl_seconds=TTL, clock=rig.wall.clock(), store=FileRecordStore(path)
        )
        assert registry.records == {}
        assert any("unreadable page ledger" in m for m in _warnings(loguru_records)), garbage

    d = _daemon_on_file(rig, FileRecordStore(path))
    assert await page_lifecycle.reap_expired_pages(d) == 0
    # meiva:note
    # adopted, file rewritten
    assert [r.owner for r in FileRecordStore(path).load().values()] == [9]


async def test_an_entry_with_a_bad_field_rejects_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "browser-pages.json"
    bad_entries: list[Callable[[], str]] = [
        lambda: '{"version": 1, "pages": {"T1": {"owner": true, "expires_at": 1}}}',
        lambda: '{"version": 1, "pages": {"T1": {"owner": 7, "expires_at": "soon"}}}',
        lambda: '{"version": 1, "pages": {"T1": {"owner": 7, "expires_at": NaN}}}',
        lambda: '{"version": 1, "pages": {"T1": {"owner": -1, "expires_at": 1}}}',
        lambda: '{"version": 1, "pages": {"": {"owner": 7, "expires_at": 1}}}',
    ]
    for make in bad_entries:
        path.write_text(make(), encoding="utf-8")
        assert FileRecordStore(path).load() == {}
