# meiva:note
# An agent's selected page across browser-mcp daemon restarts.
#
# The upstream numbers its pages afresh on every start, so the daemon saves the selection as a
# CDP target id and, on the agent's first call afterwards, finds that target in the new
# upstream's listing and re-selects it. A restart is `Rig.start()` again: a new daemon and a
# new fake upstream over the same Chrome, ledger file and selection file. Restoring a
# selection grants no right to close the page; only the ledger's records do.
from __future__ import annotations

from services.desktop.browser import page_lifecycle
from services.desktop.browser.tab_groups.page_store import (
    FileSelectionStore,
    dump_selection,
    parse_selection,
)
from services.desktop.browser.tests.browser_mcp_daemon.test_page_ledger_across_restarts import (
    TTL,
    Rig,
    open_page,
)
from services.desktop.browser.tests.test_browser_mcp_daemon import (
    FakeUpstream,
    sweep_twice,
    text_of,
)

AGENT = 7


def _selected_url(up: FakeUpstream) -> str | None:
    return None if up.selected is None else up.pages[up.selected]


async def test_a_page_the_agent_created_is_selected_again_after_a_restart() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, AGENT, "https://mine.example/")
    assert list(rig.selection.selected) == [AGENT]

    d2, up2 = rig.start()
    result = await d2.call_tool_for_agent("take_snapshot", {}, AGENT)

    assert not result.is_error
    assert _selected_url(up2) == "https://mine.example/"
    assert d2.pages.get_agent_page(AGENT) == rig.browser.page_id_of("https://mine.example/")
    assert [c[0] for c in up2.calls] == ["list_pages", "select_page", "take_snapshot"]


async def test_a_tab_the_agent_selected_is_restored_without_any_right_to_close_it() -> None:
    rig = Rig()
    user_tab = rig.browser.add_tab("https://users.example/")
    d1, _ = rig.start()
    result = await d1.call_tool_for_agent(
        "select_page", {"pageId": rig.browser.page_id_of("https://users.example/")}, AGENT
    )
    assert not result.is_error
    assert rig.selection.selected == {AGENT: user_tab}

    d2, up2 = rig.start()
    assert not (await d2.call_tool_for_agent("take_snapshot", {}, AGENT)).is_error
    assert _selected_url(up2) == "https://users.example/"

    # meiva:note
    # no record, so neither a release nor a sweep may close it
    assert rig.store.records == {}
    rig.wall.advance(100 * TTL)
    assert await page_lifecycle.release_agent_page(d2, AGENT) is None
    assert await sweep_twice(d2) == 0
    assert user_tab in rig.browser.targets and rig.browser.closed == []


async def test_a_selection_whose_target_is_gone_is_dropped() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, AGENT, "https://mine.example/")
    (target,) = rig.selection.selected.values()
    rig.browser._forget(target)

    d2, _ = rig.start()
    result = await d2.call_tool_for_agent("take_snapshot", {}, AGENT)

    assert result.is_error and "No page selected" in text_of(result)
    assert rig.selection.selected == {}


async def test_a_chrome_restart_drops_the_selection_because_targets_are_new() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, AGENT, "https://mine.example/")
    rig.browser.restart_chrome()

    d2, _ = rig.start()
    result = await d2.call_tool_for_agent("take_snapshot", {}, AGENT)

    assert result.is_error and rig.selection.selected == {}


async def test_two_tabs_on_one_url_cannot_be_told_apart_so_nothing_is_restored() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, AGENT, "https://same.example/")
    rig.browser.add_tab("https://same.example/")

    d2, up2 = rig.start()
    result = await d2.call_tool_for_agent("take_snapshot", {}, AGENT)

    assert result.is_error and _selected_url(up2) is None
    assert rig.selection.selected == {}


async def test_closing_the_selected_page_forgets_the_selection() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, AGENT, "https://mine.example/")
    page = d1.pages.get_agent_page(AGENT)
    await d1.call_tool_for_agent("close_page", {"pageId": page}, AGENT)

    assert rig.selection.selected == {}


async def test_each_agent_is_restored_to_its_own_page() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, 7, "https://seven.example/")
    await open_page(d1, 8, "https://eight.example/")

    d2, up2 = rig.start()
    await d2.call_tool_for_agent("take_snapshot", {}, 8)
    assert _selected_url(up2) == "https://eight.example/"
    await d2.call_tool_for_agent("take_snapshot", {}, 7)
    assert _selected_url(up2) == "https://seven.example/"


async def test_the_selection_is_restored_once_per_daemon_process() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, AGENT, "https://mine.example/")

    d2, up2 = rig.start()
    await d2.call_tool_for_agent("take_snapshot", {}, AGENT)
    up2.calls.clear()
    await d2.call_tool_for_agent("take_snapshot", {}, AGENT)

    # meiva:note
    # the ordinary re-pin
    assert [c[0] for c in up2.calls] == ["select_page", "take_snapshot"]


async def test_an_unreadable_chrome_loses_the_restore_but_not_the_call() -> None:
    rig = Rig()
    d1, _ = rig.start()
    await open_page(d1, AGENT, "https://mine.example/")

    d2, _ = rig.start()
    rig.browser.read_error = RuntimeError("chrome unreadable")
    result = await d2.call_tool_for_agent("take_snapshot", {}, AGENT)

    assert result.is_error and "No page selected" in text_of(result)


# meiva:note
# ── The selection file ───────────────────────────────────────────────────────


def test_the_selection_file_round_trips(tmp_path: object) -> None:
    selected = {7: "ABC", 12: "DEF"}
    assert parse_selection(dump_selection(selected)) == selected


def test_a_file_store_survives_a_restart_and_reads_unreadable_files_as_empty(
    tmp_path: object,
) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "run" / "browser-selected-pages.json"
    assert FileSelectionStore(path).load() == {}
    FileSelectionStore(path).save({7: "ABC"})
    assert FileSelectionStore(path).load() == {7: "ABC"}
    for broken in (
        "not json",
        '{"version": 2, "selected": {}}',
        '{"version": 1, "selected": []}',
        '{"version": 1, "selected": {"x": "T"}}',
        '{"version": 1, "selected": {"7": 3}}',
    ):
        path.write_text(broken, encoding="utf-8")
        assert FileSelectionStore(path).load() == {}
