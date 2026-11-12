# meiva:note
# The page ledger across browser-mcp daemon restarts, Chrome restarts and corrupt state.
#
# A "daemon restart" here is a new `ChromeMcpDaemon` over the same `FakeBrowser` (Chrome
# keeps running, its page targets and tab groups stay) and the same `MemoryStore` (the
# ledger file), with a new fake upstream that numbers the open pages afresh: what a
# cluster update or an upstream `chrome-devtools-mcp` death does. The record is the basis
# for closing a page; the tab group only checks that the user has not taken it. A page
# the user took, or one in an agent group that has no record, is never closed.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from services.desktop.browser import page_lifecycle
from services.desktop.browser.mcp_daemon import ChromeMcpDaemon, text_of
from services.desktop.browser.page_lifecycle import FileRecordStore
from services.desktop.browser.tab_groups.page_ledger import PageRecord
from services.desktop.browser.tests.fake_browser import FakeBrowser, MemorySelection, MemoryStore
from services.desktop.browser.tests.test_browser_mcp_daemon import (
    FakeUpstream,
    new_daemon,
    sweep_twice,
)

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
# The ledger starts as one whose migration is done, unless a test asks for a first run.
@dataclass
class Rig:
    browser: FakeBrowser = field(default_factory=FakeBrowser)
    store: MemoryStore = field(default_factory=lambda: MemoryStore(migrated=True))
    wall: Wall = field(default_factory=Wall)
    selection: MemorySelection = field(default_factory=MemorySelection)

    def start(self) -> tuple[ChromeMcpDaemon, FakeUpstream]:
        up = FakeUpstream()
        self.browser.rebind(up)
        daemon = new_daemon(
            up,
            tab_groups=self.browser,
            default_ttl_seconds=TTL,
            store=self.store,
            selection=self.selection,
            clock=self.wall.clock(),
        )
        return daemon, up


async def open_page(d: ChromeMcpDaemon, agent_id: int, url: str, **args: str) -> None:
    result = await d.call_tool_for_agent("new_page", {"url": url, **args}, agent_id)
    assert not result.is_error


async def _sweep(d: ChromeMcpDaemon) -> int:
    return await page_lifecycle.reap_expired_pages(d)


def _open_urls(rig: Rig) -> set[str]:
    return set(rig.browser.targets.values())


def _messages(records: list[dict[str, Any]], level: str) -> list[str]:
    return [r["message"] for r in records if r["level"].name == level]


# meiva:note
# ── The record survives a daemon restart ────────────────────────────────────


async def test_a_page_created_before_a_daemon_restart_is_swept_after_its_deadline() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://a.example/")
    assert len(rig.store.records) == 1

    # meiva:note
    # the daemon restarted; Chrome and the ledger file did not
    d2, up2 = rig.start()
    assert d2.pages.page_targets == {}
    rig.wall.advance(TTL - 1)
    assert await sweep_twice(d2) == 0
    rig.wall.advance(2)
    assert await sweep_twice(d2) == 1

    assert _open_urls(rig) == {"about:blank"}
    assert up2.pages == {1: "about:blank"}
    assert rig.store.records == {}


async def test_release_after_a_daemon_restart_closes_every_page_of_that_agent_only() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://a1.example/")
    await open_page(d1, 7, "https://a2.example/")
    await open_page(d1, 8, "https://b.example/")
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
    await open_page(d1, 7, "https://a.example/")

    d2, _ = rig.start()
    # meiva:note
    # records the URL the page shows
    await _sweep(d2)
    d2.pages.set_agent_page(7, rig.browser.page_id_of("https://a.example/"))
    result = await d2.call_tool_for_agent("renew_page", {"ttl": 2 * TTL}, 7)
    assert not result.is_error, text_of(result)

    rig.wall.advance(TTL + 1)
    assert await sweep_twice(d2) == 0
    rig.wall.advance(TTL + 1)
    assert await sweep_twice(d2) == 1


async def test_renew_after_a_restart_refuses_an_ambiguous_page_instead_of_guessing() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://same.example/")
    await open_page(d1, 7, "https://same.example/")

    d2, up2 = rig.start()
    await _sweep(d2)
    d2.pages.set_agent_page(7, 2)
    result = await d2.call_tool_for_agent("renew_page", {"ttl": 60}, 7)
    assert result.is_error and "no page TTL" in text_of(result)
    assert up2.pages[2] == "https://same.example/"


async def test_renew_after_a_restart_refuses_a_page_the_agent_does_not_own() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 8, "https://b.example/")
    rig.browser.add_tab("https://user.example/")

    d2, _ = rig.start()
    await _sweep(d2)
    for url in ("https://b.example/", "https://user.example/"):
        d2.pages.set_agent_page(7, rig.browser.page_id_of(url))
        assert (await d2.call_tool_for_agent("renew_page", {"ttl": 60}, 7)).is_error, url


async def test_a_record_of_a_page_chrome_no_longer_has_is_dropped() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://a.example/")
    (target,) = rig.store.records
    # meiva:note
    # the user closed the tab by hand while the daemon was down
    rig.browser.targets.pop(target)

    d2, _ = rig.start()
    assert await _sweep(d2) == 0
    assert rig.store.records == {}


# meiva:note
# ── Pages that never get a group still have a deadline ──────────────────────


async def test_an_isolated_context_page_is_recorded_expires_and_is_released() -> None:
    rig = Rig()
    d, up = rig.start()
    await open_page(d, 7, "https://discord.com/app", isolatedContext="reg-7")
    await open_page(d, 8, "https://other.example/", isolatedContext="reg-8")
    assert up.isolated_contexts == {2: "reg-7", 3: "reg-8"}
    # meiva:note
    # never grouped
    assert rig.browser.titles == {}
    records = rig.store.records
    assert sorted((r.owner, r.grouped) for r in records.values()) == [(7, False), (8, False)]

    await page_lifecycle.release_agent_page(d, 8)
    assert "https://other.example/" not in _open_urls(rig)
    rig.wall.advance(TTL + 1)
    assert await sweep_twice(d) == 1
    assert _open_urls(rig) == {"about:blank"}


async def test_a_page_whose_grouping_failed_has_a_record_and_is_closed_at_its_deadline() -> None:
    rig = Rig()
    rig.browser.grouping_fails = True
    d, _ = rig.start()
    await open_page(d, 7, "https://a.example/")
    (record,) = rig.store.records.values()
    assert not record.grouped and rig.browser.titles == {}

    rig.wall.advance(TTL + 1)
    assert await sweep_twice(d) == 1
    assert _open_urls(rig) == {"about:blank"}


async def test_the_background_grouping_marks_the_record_grouped() -> None:
    rig = Rig()
    d, _ = rig.start()
    await open_page(d, 7, "https://a.example/")
    (record,) = rig.store.records.values()
    assert record.grouped


# meiva:note
# ── The user takes a page ───────────────────────────────────────────────────


async def test_a_grouped_page_the_user_dragged_out_of_its_group_is_never_closed() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://mine.example/")
    (target,) = rig.store.records
    rig.browser.titles[target] = "Work"

    d2, _ = rig.start()
    rig.wall.advance(10 * TTL)
    for _ in range(3):
        await _sweep(d2)
        await page_lifecycle.release_agent_page(d2, 7)
    assert target in rig.browser.targets and rig.browser.closed == []
    assert rig.store.records == {}


async def test_a_page_taken_between_the_reconciliation_and_the_close_stays_open() -> None:
    rig = Rig()
    d, _ = rig.start()
    await open_page(d, 7, "https://mine.example/")
    (target,) = rig.store.records
    rig.wall.advance(TTL + 1)
    # meiva:note
    # marks it due
    await _sweep(d)

    def take_it(read: int) -> None:
        # meiva:note
        # the re-check read
        if read == rig.browser.state_reads and read % 2 == 0:
            rig.browser.titles[target] = "Work"

    rig.browser.on_state_read = take_it
    assert await _sweep(d) == 0
    assert target in rig.browser.targets and rig.browser.closed == []
    assert rig.store.records == {}


async def test_the_browsers_last_page_is_not_closed_even_if_it_became_the_last_after_the_read() -> (
    None
):
    rig = Rig()
    d, _ = rig.start()
    await open_page(d, 7, "https://mine.example/")
    rig.wall.advance(TTL + 1)
    await _sweep(d)

    def blank_tab_closed(read: int) -> None:
        if read % 2 == 0:
            rig.browser.targets = {
                t: u for t, u in rig.browser.targets.items() if u != "about:blank"
            }

    rig.browser.on_state_read = blank_tab_closed
    assert await _sweep(d) == 0
    assert _open_urls(rig) == {"https://mine.example/"} and len(rig.store.records) == 1


# meiva:note
# ── No routine adoption; the one-time migration ─────────────────────────────


async def test_a_grouped_page_without_a_record_is_never_adopted_or_closed(
    loguru_records: list[dict[str, Any]],
) -> None:
    rig = Rig()
    stray = rig.browser.add_tab("https://stray.example/", title="Meiva #9")
    d, _ = rig.start()
    rig.wall.advance(100 * TTL)
    for _ in range(4):
        assert await _sweep(d) == 0
    assert stray in rig.browser.targets and rig.store.records == {}
    assert any("no ledger record" in m for m in _messages(loguru_records, "INFO"))


async def test_the_first_run_migrates_pages_in_agent_groups_once_and_logs_each(
    loguru_records: list[dict[str, Any]],
) -> None:
    # meiva:note
    # no ledger file yet: the first run of this mechanism
    rig = Rig(store=MemoryStore())
    rig.browser.add_tab("https://old.example/secret/path?token=x", title="Meiva #9")
    rig.browser.add_tab("https://old2.example/", title="Meiva #4")
    d, _ = rig.start()

    assert await _sweep(d) == 0
    assert sorted(r.owner for r in rig.store.records.values()) == [4, 9]
    assert rig.store.file is not None and rig.store.file.migrated
    migration = [m for m in _messages(loguru_records, "INFO") if "migration adopted" in m]
    assert len(migration) == 2
    assert any("agent 9" in m and "https://old.example" in m for m in migration)
    assert not any("secret" in m or "token" in m for m in migration)

    later = rig.browser.add_tab("https://later.example/", title="Meiva #9")
    # meiva:note
    # a later daemon: the migration is not run again
    d2, _ = rig.start()
    assert await _sweep(d2) == 0 and later not in rig.store.records

    rig.wall.advance(TTL + 1)
    assert await sweep_twice(d2) == 2
    assert later in rig.browser.targets


async def test_a_corrupt_ledger_file_reads_as_empty_and_migrates_nothing(
    tmp_path: Path, loguru_records: list[dict[str, Any]]
) -> None:
    path = tmp_path / "browser-pages.json"
    rig = Rig()
    rig.browser.add_tab("https://old.example/", title="Meiva #9")
    for garbage in ("{not json", "[]", '{"version": 2, "pages": {}}', "", '{"version": 1}'):
        path.write_text(garbage, encoding="utf-8")
        loguru_records.clear()
        loaded = FileRecordStore(path).load()
        assert loaded is not None and loaded.records == {} and loaded.migrated
        assert any("unreadable page ledger" in m for m in _messages(loguru_records, "WARNING"))

    d = _daemon_on_file(rig, FileRecordStore(path))
    rig.wall.advance(100 * TTL)
    assert await sweep_twice(d) == 0
    assert FileRecordStore(path).load() == FileRecordStore(path).load()
    assert rig.browser.closed == []


def _daemon_on_file(rig: Rig, store: FileRecordStore) -> ChromeMcpDaemon:
    up = FakeUpstream()
    rig.browser.rebind(up)
    return new_daemon(
        up, tab_groups=rig.browser, default_ttl_seconds=TTL, store=store, clock=rig.wall.clock()
    )


# meiva:note
# ── Chrome restart: inheritance ─────────────────────────────────────────────


async def test_a_chrome_restart_passes_the_record_with_its_original_deadline() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://a.example/")
    # meiva:note
    # the record learns the URL
    await _sweep(d1)
    rig.wall.advance(TTL - 100)
    # meiva:note
    # restored tabs and groups, new target ids
    rig.browser.restart_chrome()

    d2, _ = rig.start()
    assert await _sweep(d2) == 0
    (record,) = rig.store.records.values()
    assert record.owner == 7 and record.expires_at == d1.pages.now() + 100
    rig.wall.advance(101)
    assert await sweep_twice(d2) == 1
    assert _open_urls(rig) == {"about:blank"}


async def test_a_chrome_restart_with_several_candidates_inherits_nothing_and_closes_nothing() -> (
    None
):
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://same.example/")
    await open_page(d1, 7, "https://same.example/")
    await _sweep(d1)
    rig.browser.restart_chrome()

    d2, _ = rig.start()
    rig.wall.advance(100 * TTL)
    for _ in range(3):
        assert await _sweep(d2) == 0
    assert rig.store.records == {}
    assert sorted(rig.browser.targets.values()) == ["about:blank"] + ["https://same.example/"] * 2


async def test_a_users_tab_with_the_same_url_put_in_a_group_by_a_layout_restore_is_not_closed() -> (
    None
):
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://x.example/")
    # meiva:note
    # the user has the same page open
    user_tab = rig.browser.add_tab("https://x.example/")
    await _sweep(d1)
    rig.browser.restart_chrome()
    # meiva:note
    # the layout restore filed the first tab with that URL it found: the user's
    (agent_tab, restored_user_tab) = [
        t for t, u in rig.browser.targets.items() if u == "https://x.example/"
    ]
    rig.browser.titles.pop(agent_tab, None)
    rig.browser.titles[restored_user_tab] = "Meiva #7"
    assert user_tab not in rig.browser.targets

    d2, _ = rig.start()
    rig.wall.advance(100 * TTL)
    for _ in range(4):
        assert await _sweep(d2) == 0
    assert {agent_tab, restored_user_tab} <= set(rig.browser.targets)
    assert rig.store.records == {} and rig.browser.closed == []


# meiva:note
# ── Sweep safeguards ────────────────────────────────────────────────────────


async def test_the_first_sweep_after_a_start_closes_nothing_and_a_transient_step_is_forgiven() -> (
    None
):
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://a.example/")

    d2, _ = rig.start()
    # meiva:note
    # a wall clock stepped far forward
    rig.wall.advance(10 * TTL)
    # meiva:note
    # the first sweep of this daemon only marks
    assert await _sweep(d2) == 0
    # meiva:note
    # the step is corrected before the next sweep
    rig.wall.advance(-10 * TTL)
    assert await _sweep(d2) == 0
    assert d2.pages.due_seen == set() and len(rig.store.records) == 1
    # meiva:note
    # a real expiry is found due by two consecutive sweeps
    rig.wall.advance(10 * TTL)
    assert await _sweep(d2) == 0
    assert await _sweep(d2) == 1


async def test_a_clock_stepped_backwards_never_closes_a_page_early_and_bounds_the_wait() -> None:
    rig = Rig()
    d, _ = rig.start()
    await open_page(d, 7, "https://a.example/")
    rig.wall.advance(-10 * 86_400)

    assert await sweep_twice(d) == 0
    (record,) = rig.store.records.values()
    assert record.expires_at == d.pages.now() + 86_400.0
    rig.wall.advance(86_401)
    assert await sweep_twice(d) == 1


async def test_a_close_that_leaves_the_target_open_keeps_the_record_and_logs_sparingly(
    loguru_records: list[dict[str, Any]],
) -> None:
    rig = Rig()
    d, _ = rig.start()
    await open_page(d, 7, "https://a.example/")
    (target,) = rig.store.records
    # meiva:note
    # e.g. a beforeunload prompt
    rig.browser.stays_open.add(target)
    rig.wall.advance(TTL + 1)
    (before,) = rig.store.records.values()

    for _ in range(8):
        assert await _sweep(d) == 0
    # meiva:note
    # no dropped record, no new deadline
    (after,) = rig.store.records.values()
    assert (after.owner, after.expires_at) == (before.owner, before.expires_at)
    failures = [m for m in _messages(loguru_records, "WARNING") if "could not close page" in m]
    assert len(failures) == 2 and "failure 1)" in failures[0] and "failure 6)" in failures[1]
    assert all(
        r["exception"] is None for r in loguru_records if "could not close page" in r["message"]
    )

    rig.browser.stays_open.clear()
    assert await _sweep(d) == 1


async def test_a_page_whose_last_tab_cannot_be_ungrouped_is_not_closed() -> None:
    rig = Rig()
    d, _ = rig.start()
    await open_page(d, 7, "https://a.example/")
    rig.wall.advance(TTL + 1)
    rig.browser.ungroup_error = TimeoutError("worker wedged")

    assert await sweep_twice(d) == 0 and rig.browser.closed == []
    rig.browser.ungroup_error = None
    assert await _sweep(d) == 1


async def test_an_unreadable_chrome_closes_nothing_and_keeps_the_records(
    loguru_records: list[dict[str, Any]],
) -> None:
    rig = Rig()
    d, _ = rig.start()
    await open_page(d, 7, "https://a.example/")
    rig.wall.advance(TTL + 1)
    await _sweep(d)
    rig.browser.read_error = TimeoutError("extension wedged")

    assert await _sweep(d) == 0
    assert await page_lifecycle.release_agent_page(d, 7) is None
    assert rig.browser.closed == [] and len(rig.store.records) == 1
    assert any("could not read Chrome's tabs" in m for m in _messages(loguru_records, "WARNING"))
    rig.browser.read_error = None
    # meiva:note
    # the pass that could not read Chrome broke the run
    assert d.pages.due_seen == set()
    assert await sweep_twice(d) == 1


async def test_a_close_chrome_refuses_keeps_the_record_and_is_retried() -> None:
    rig = Rig()
    d, _ = rig.start()
    await open_page(d, 7, "https://a.example/")
    rig.wall.advance(TTL + 1)
    rig.browser.close_error = RuntimeError("Chrome said no")

    assert await sweep_twice(d) == 0 and len(rig.store.records) == 1
    rig.browser.close_error = None
    assert await _sweep(d) == 1 and rig.store.records == {}


async def test_the_last_page_of_the_browser_is_never_closed() -> None:
    rig = Rig()
    rig.browser = FakeBrowser(blank_tab=False)
    d, _ = rig.start()
    await open_page(d, 7, "https://only.example/")
    rig.wall.advance(TTL + 1)

    assert await sweep_twice(d) == 0
    assert await page_lifecycle.release_agent_page(d, 7) is None
    assert _open_urls(rig) == {"https://only.example/"}
    rig.browser.add_tab("https://another.example/")
    assert await _sweep(d) == 1


async def test_user_tabs_and_stale_records_naming_them_are_never_closed_in_any_path() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://mine.example/")
    plain = rig.browser.add_tab("https://plain.example/")
    in_work = rig.browser.add_tab("https://work.example/", title="Work")
    bad_title = rig.browser.add_tab("https://bad.example/", title="Meiva #07")
    stray = rig.browser.add_tab("https://stray.example/", title="Meiva #7")
    # meiva:note
    # grouped records naming tabs that are not in their owner's group: the user's, not ours
    stale = {t: PageRecord(7, 0.0, grouped=True) for t in (plain, in_work, bad_title)}
    rig.store = MemoryStore({**rig.store.records, **stale}, migrated=True)

    d2, _ = rig.start()
    rig.wall.advance(10 * TTL)
    for _ in range(3):
        await _sweep(d2)
        await page_lifecycle.release_agent_page(d2, 7)
        await page_lifecycle.release_agent_page(d2, 9)

    assert {plain, in_work, bad_title, stray} <= set(rig.browser.targets)
    # meiva:note
    # only the recorded page of agent 7 went
    assert "https://mine.example/" not in _open_urls(rig)
    assert rig.store.records == {}


# meiva:note
# ── The ledger file ─────────────────────────────────────────────────────────


async def test_the_ledger_file_is_written_on_create_group_renew_and_close(tmp_path: Path) -> None:
    path = tmp_path / "run" / "browser-pages.json"
    rig = Rig()
    d = _daemon_on_file(rig, FileRecordStore(path))
    await open_page(d, 7, "https://a.example/")
    assert path.exists() and oct(path.stat().st_mode & 0o777) == "0o600"
    (record,) = (FileRecordStore(path).load() or _empty()).records.values()
    assert record.grouped and record.owner == 7

    await d.call_tool_for_agent("renew_page", {"ttl": 100}, 7)
    (record,) = (FileRecordStore(path).load() or _empty()).records.values()
    assert record.expires_at == rig.wall.moment.timestamp() + 100

    await d.call_tool_for_agent("close_page", {"pageId": 2}, 7)
    assert (FileRecordStore(path).load() or _empty()).records == {}


def _empty() -> Any:
    raise AssertionError("the ledger file should exist")


async def test_an_entry_with_a_bad_field_rejects_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "browser-pages.json"
    head = '{"version": 1, "migrated": true, "pages": {'
    for entry in (
        '"T1": {"owner": true, "expires_at": 1, "grouped": true, "url": ""}',
        '"T1": {"owner": 7, "expires_at": "soon", "grouped": true, "url": ""}',
        '"T1": {"owner": 7, "expires_at": NaN, "grouped": true, "url": ""}',
        '"T1": {"owner": -1, "expires_at": 1, "grouped": true, "url": ""}',
        '"": {"owner": 7, "expires_at": 1, "grouped": true, "url": ""}',
        '"T1": {"owner": 7, "expires_at": 1}',
    ):
        path.write_text(head + entry + "}}", encoding="utf-8")
        loaded = FileRecordStore(path).load()
        assert loaded is not None and loaded.records == {} and loaded.migrated


# meiva:note
# ── Review hardening ────────────────────────────────────────────────────────


# meiva:note
# An upstream whose `new_page` is raced by a tab the user opens at the same moment.
class _RacyUpstream(FakeUpstream):
    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        result = await super().call_tool(name, args)
        if name == "new_page" and self.browser is not None:
            self.browser.add_tab("https://user-opened.example/")
        return result


async def test_a_new_page_raced_by_another_new_tab_gets_no_record_grouped_or_isolated() -> None:
    rig = Rig()
    up = _RacyUpstream()
    rig.browser.rebind(up)
    d = new_daemon(
        up,
        tab_groups=rig.browser,
        default_ttl_seconds=TTL,
        store=rig.store,
        clock=rig.wall.clock(),
    )
    await open_page(d, 7, "https://a.example/")
    await open_page(d, 7, "https://iso.example/", isolatedContext="reg-7")
    assert rig.store.records == {} and d.pages.records == {}
    # meiva:note
    # the pages were created and selected
    assert d.pages.get_agent_page(7) == up.selected


async def test_the_migration_is_decided_at_start_so_an_early_write_ends_it() -> None:
    # meiva:note
    # no ledger file when the first daemon starts
    rig = Rig(store=MemoryStore())
    stray = rig.browser.add_tab("https://stray.example/", title="Meiva #9")
    d1, _ = rig.start()
    # meiva:note
    # writes the ledger before any reconciliation
    await open_page(d1, 7, "https://a.example/")
    assert rig.store.file is not None and rig.store.file.migrated

    # meiva:note
    # the first daemon died before its first sweep
    d2, _ = rig.start()
    assert await _sweep(d2) == 0
    assert stray not in rig.store.records and len(rig.store.records) == 1


async def test_a_migration_that_adopted_nothing_still_marks_the_ledger() -> None:
    rig = Rig(store=MemoryStore())
    d, _ = rig.start()
    await _sweep(d)
    assert rig.store.file is not None and rig.store.file.migrated
    late = rig.browser.add_tab("https://late.example/", title="Meiva #9")
    d2, _ = rig.start()
    await _sweep(d2)
    assert late not in rig.store.records


async def test_a_migrated_record_is_left_to_the_sweep_not_to_a_release() -> None:
    rig = Rig(store=MemoryStore())
    stray = rig.browser.add_tab("https://stray.example/", title="Meiva #9")
    d, _ = rig.start()
    await _sweep(d)
    assert rig.store.records[stray].adopted

    assert await page_lifecycle.release_agent_page(d, 9) is None
    assert stray in rig.browser.targets
    rig.wall.advance(TTL + 1)
    assert await sweep_twice(d) == 1
    assert stray not in rig.browser.targets


async def test_an_ungrouped_page_the_user_filed_into_their_own_group_is_never_closed() -> None:
    rig = Rig()
    d, _ = rig.start()
    await open_page(d, 7, "https://discord.com/app", isolatedContext="reg-7")
    (target,) = rig.store.records
    rig.browser.titles[target] = "Work"
    rig.wall.advance(10 * TTL)

    for _ in range(3):
        assert await _sweep(d) == 0
    await page_lifecycle.release_agent_page(d, 7)
    assert target in rig.browser.targets and rig.store.records == {}


async def test_an_unreadable_recheck_ends_the_whole_pass_after_one_failed_read() -> None:
    rig = Rig()
    d, _ = rig.start()
    await open_page(d, 7, "https://a.example/")
    await open_page(d, 7, "https://b.example/")
    rig.wall.advance(TTL + 1)
    # meiva:note
    # marks both due (read 1)
    assert await _sweep(d) == 0
    reads_before = rig.browser.state_reads

    def wedge_on_recheck(read: int) -> None:
        # meiva:note
        # the first page's re-check, right after the reconciliation
        if read == reads_before + 2:
            rig.browser.read_error = TimeoutError("extension wedged")

    rig.browser.on_state_read = wedge_on_recheck
    assert await _sweep(d) == 0
    # meiva:note
    # not one timeout per page
    assert rig.browser.state_reads == reads_before + 2
    assert rig.browser.closed == [] and d.pages.due_seen == set()

    rig.browser.read_error = None
    rig.browser.on_state_read = None
    assert await sweep_twice(d) == 2
