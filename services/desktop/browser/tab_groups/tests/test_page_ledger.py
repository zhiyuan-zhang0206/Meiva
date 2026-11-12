# meiva:note
# The page ledger's pure decisions: what a record proves, what the tab group only
# checks, when a record is inherited or migrated, and the last check before a close.
from __future__ import annotations

import pytest

from services.desktop.browser.tab_groups.grouping import agent_id_of_title, group_title
from services.desktop.browser.tab_groups.page_ledger import (
    LedgerFile,
    PageRecord,
    Reconciled,
    TabState,
    agent_pages,
    may_close,
    reconcile,
    target_of_url,
)
from services.desktop.browser.tab_groups.page_store import dump_records, parse_records

NOW = 1_000_000.0
TTL = 3_600.0


def _tabs(pages: dict[str, str], groups: dict[str, int] | None = None) -> TabState:
    return TabState(pages=pages, agent_groups=groups or {})


def _rec(
    owner: int = 7, expires_at: float = NOW + 100, *, grouped: bool = True, url: str = ""
) -> PageRecord:
    return PageRecord(owner, expires_at, grouped=grouped, url=url)


def _reconcile(
    records: dict[str, PageRecord], tabs: TabState, *, now: float = NOW, migrate: bool = False
) -> Reconciled:
    return reconcile(records, tabs, now=now, default_ttl_seconds=TTL, migrate=migrate)


# meiva:note
# ── The group title is the identity ─────────────────────────────────────────


def test_agent_id_of_title_is_the_exact_inverse_of_group_title() -> None:
    for agent_id in (0, 7, 405, 10**6):
        assert agent_id_of_title(group_title(agent_id)) == agent_id


@pytest.mark.parametrize(
    "title",
    [
        None,
        "",
        "Work",
        "Meiva #",
        "Meiva #x",
        "Meiva #07",
        "Meiva #7 ",
        " Meiva #7",
        "meiva #7",
        "Meiva #-1",
        "Meiva #+7",
        "Meiva #7.0",
        "Meiva #٣",
        "Meiva #7 (old)",
        7,
    ],
)
def test_agent_id_of_title_rejects_every_other_title(title: object) -> None:
    assert agent_id_of_title(title) is None


# meiva:note
# ── The record proves the page is ours; the group only says the user has not taken it ──


def test_an_ungrouped_record_is_due_at_its_deadline_without_any_group() -> None:
    tabs = _tabs({"T1": "https://iso/", "T2": "https://u/"})
    live = _reconcile({"T1": _rec(grouped=False, expires_at=NOW + 5)}, tabs)
    assert live.due == ()
    due = _reconcile({"T1": _rec(grouped=False, expires_at=NOW - 1)}, tabs)
    assert [(p.target_id, p.owner, p.grouped) for p in due.due] == [("T1", 7, False)]
    assert "T1" in due.records


def test_a_record_becomes_grouped_the_first_time_chrome_shows_it_in_its_owners_group() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://u/"}, {"T1": 7})
    result = _reconcile({"T1": _rec(grouped=False)}, tabs)
    assert result.records["T1"].grouped


def test_a_grouped_page_that_left_its_owners_group_is_taken_and_never_due() -> None:
    # meiva:note
    # in no group, or in another agent's group
    for groups in ({}, {"T1": 9}):
        tabs = _tabs({"T1": "https://a/", "T2": "https://u/"}, groups)
        result = _reconcile({"T1": _rec(expires_at=NOW - 1)}, tabs)
        assert result.taken == ("T1",)
        assert result.records == {} and result.due == ()


def test_a_due_grouped_page_in_its_group_is_due() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://u/"}, {"T1": 7})
    result = _reconcile({"T1": _rec(expires_at=NOW - 1)}, tabs)
    assert [p.target_id for p in result.due] == ["T1"]


def test_a_record_exactly_at_its_deadline_is_due_and_one_second_before_is_not() -> None:
    tabs = _tabs({"T1": "https://a/"}, {"T1": 7})
    assert _reconcile({"T1": _rec(expires_at=NOW)}, tabs).due
    assert not _reconcile({"T1": _rec(expires_at=NOW + 1)}, tabs).due


def test_a_record_of_a_target_chrome_lacks_is_dropped() -> None:
    tabs = _tabs({"T1": "https://a/"}, {"T1": 7})
    result = _reconcile({"T1": _rec(), "T9": _rec()}, tabs)
    assert set(result.records) == {"T1"}
    assert result.vanished == ("T9",)


def test_the_url_of_a_record_follows_the_page() -> None:
    tabs = _tabs({"T1": "https://now/"}, {"T1": 7})
    assert _reconcile({"T1": _rec(url="https://before/")}, tabs).records["T1"].url == "https://now/"


# meiva:note
# ── No routine adoption ─────────────────────────────────────────────────────


def test_a_page_in_an_agent_group_without_a_record_is_not_adopted_or_closed() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://b/"}, {"T1": 7, "T2": 9})
    result = _reconcile({}, tabs)
    assert result.records == {} and result.adopted == () and result.due == ()
    assert result.unrecorded == 2


def test_a_page_in_no_group_without_a_record_is_not_looked_at() -> None:
    result = _reconcile({}, _tabs({"T1": "https://user/", "T2": "https://work/"}))
    assert result.records == {} and result.unrecorded == 0 and result.due == ()


# meiva:note
# ── The one-time migration ──────────────────────────────────────────────────


def test_the_migration_adopts_grouped_unrecorded_pages_with_a_fresh_ttl() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://b/", "T3": "https://u/"}, {"T1": 7, "T2": 9})
    result = _reconcile({}, tabs, migrate=True)
    assert result.records == {
        "T1": PageRecord(7, NOW + TTL, grouped=True, url="https://a/"),
        "T2": PageRecord(9, NOW + TTL, grouped=True, url="https://b/"),
    }
    assert result.adopted == (("T1", 7), ("T2", 9))
    assert result.migrated and result.unrecorded == 0


def test_the_migration_leaves_recorded_pages_and_user_tabs_alone() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://u/"}, {"T1": 7})
    result = _reconcile({"T1": _rec(expires_at=NOW + 5)}, tabs, migrate=True)
    assert result.adopted == () and result.records["T1"].expires_at == NOW + 5


# meiva:note
# ── Inheritance after a Chrome restart ──────────────────────────────────────


def test_a_gone_record_passes_to_the_one_restored_page_with_the_same_owner_and_url() -> None:
    tabs = _tabs({"N1": "https://a/", "U": "https://u/"}, {"N1": 7})
    result = _reconcile({"OLD": _rec(expires_at=NOW + 42, url="https://a/")}, tabs)
    assert result.inherited == (("OLD", "N1", 7),)
    assert result.records == {"N1": PageRecord(7, NOW + 42, grouped=True, url="https://a/")}
    assert result.vanished == ()


def test_several_candidates_or_several_gone_records_never_inherit() -> None:
    two_pages = _tabs({"N1": "https://a/", "N2": "https://a/", "U": "u"}, {"N1": 7, "N2": 7})
    result = _reconcile({"OLD": _rec(url="https://a/")}, two_pages)
    assert result.inherited == () and result.records == {} and result.unrecorded == 2
    two_records = _tabs({"N1": "https://a/", "U": "u"}, {"N1": 7})
    result = _reconcile({"O1": _rec(url="https://a/"), "O2": _rec(url="https://a/")}, two_records)
    assert result.inherited == () and result.records == {}


@pytest.mark.parametrize(
    ("old", "new_url", "new_owner"),
    [
        (_rec(url="https://a/"), "https://b/", 7),
        (_rec(url="https://a/"), "https://a/", 9),
        (_rec(url="https://a/", grouped=False), "https://a/", 7),
        (_rec(url=""), "", 7),
    ],
)
def test_inheritance_needs_the_same_owner_and_url_and_a_grouped_record(
    old: PageRecord, new_url: str, new_owner: int
) -> None:
    tabs = _tabs({"N1": new_url, "U": "u"}, {"N1": new_owner})
    assert _reconcile({"OLD": old}, tabs).inherited == ()


def test_inheritance_is_refused_when_another_page_of_chrome_shows_the_same_url() -> None:
    # meiva:note
    # a user's tab with that URL could be the one a layout restore put in the group
    tabs = _tabs({"N1": "https://a/", "USER": "https://a/"}, {"N1": 7})
    result = _reconcile({"OLD": _rec(url="https://a/")}, tabs)
    assert result.inherited == () and result.records == {} and result.unrecorded == 1


def test_an_inherited_record_without_migration_still_does_not_adopt_the_others() -> None:
    tabs = _tabs({"N1": "https://a/", "N2": "https://b/", "U": "u"}, {"N1": 7, "N2": 7})
    result = _reconcile({"OLD": _rec(url="https://a/")}, tabs)
    assert set(result.records) == {"N1"} and result.unrecorded == 1


# meiva:note
# ── Clock steps ─────────────────────────────────────────────────────────────


def test_a_deadline_beyond_one_grant_ahead_is_pulled_back_after_a_clock_step_back() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "u"}, {"T1": 7})
    result = _reconcile({"T1": _rec(expires_at=NOW + 10 * 86_400)}, tabs)
    assert result.records["T1"].expires_at == NOW + 86_400 and result.due == ()


def test_a_configured_ttl_longer_than_a_day_keeps_its_own_ceiling() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "u"}, {"T1": 7})
    result = reconcile({}, tabs, now=NOW, default_ttl_seconds=3 * 86_400, migrate=True)
    assert result.records["T1"].expires_at == NOW + 3 * 86_400


def test_a_stepped_back_clock_never_makes_a_record_due() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "u"}, {"T1": 7})
    for now in (NOW - 1, NOW - 86_400, NOW - 10**7):
        assert _reconcile({"T1": _rec(expires_at=NOW + 5)}, tabs, now=now).due == ()


# meiva:note
# ── The last check before a close ───────────────────────────────────────────


def test_may_close_rechecks_presence_group_and_the_last_page() -> None:
    grouped, ungrouped = _rec(), _rec(grouped=False)
    two = _tabs({"T1": "a", "T2": "b"}, {"T1": 7})
    assert may_close(grouped, "T1", two) and may_close(ungrouped, "T1", two)
    # meiva:note
    # gone
    assert not may_close(grouped, "T9", two)
    # meiva:note
    # the user took it out of the group
    assert not may_close(grouped, "T2", two)
    # meiva:note
    # an ungrouped record never depended on the group
    assert may_close(ungrouped, "T2", two)
    # meiva:note
    # the browser's last page
    assert not may_close(grouped, "T1", _tabs({"T1": "a"}, {"T1": 7}))
    # meiva:note
    # another's group
    assert not may_close(grouped, "T1", _tabs({"T1": "a", "T2": "b"}, {"T1": 9}))


def test_agent_pages_are_the_agents_recorded_pages_chrome_still_has() -> None:
    tabs = _tabs({"T1": "a", "T2": "b", "T3": "c"}, {"T1": 7})
    records = {"T1": _rec(), "T2": _rec(grouped=False), "T3": _rec(owner=8), "T9": _rec()}
    assert [p.target_id for p in agent_pages(records, tabs, 7)] == ["T1", "T2"]
    assert agent_pages(records, tabs, 99) == []


def test_target_of_url_needs_exactly_one_match_among_the_agents_own_recorded_pages() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://a/", "T3": "https://b/", "T4": "https://c/"})
    records = {t: _rec(owner=8 if t == "T4" else 7) for t in ("T1", "T2", "T3", "T4")}
    assert target_of_url(records, tabs, 7, "https://b/") == "T3"
    assert target_of_url(records, tabs, 7, "https://a/") is None
    assert target_of_url(records, tabs, 7, "https://c/") is None


# meiva:note
# ── The file format ─────────────────────────────────────────────────────────


def test_a_ledger_round_trips_through_the_file_format() -> None:
    file = LedgerFile(
        {"T2": _rec(8, 12.5, grouped=False, url="https://x/"), "T1": _rec(7, 1.7e9, url="u")},
        migrated=True,
    )
    assert parse_records(dump_records(file)) == file
    assert parse_records(dump_records(LedgerFile({}, migrated=False))) == LedgerFile(
        {}, migrated=False
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "null",
        "[]",
        '{"migrated": true, "pages": {}}',
        '{"version": 2, "migrated": true, "pages": {}}',
        '{"version": 1, "pages": {}}',
        '{"version": 1, "migrated": "yes", "pages": {}}',
        '{"version": 1, "migrated": true, "pages": []}',
        '{"version": 1, "migrated": true, "pages": {"T1": []}}',
        '{"version": 1, "migrated": true, "pages": {"T1": {"owner": 7, "expires_at": 1}}}',
        '{"version": 1, "migrated": true, "pages": {"T1": {"owner": 7.5, "expires_at": 1, "grouped": true, "url": ""}}}',
        '{"version": 1, "migrated": true, "pages": {"T1": {"owner": 7, "expires_at": NaN, "grouped": true, "url": ""}}}',
        '{"version": 1, "migrated": true, "pages": {"T1": {"owner": 7, "expires_at": 1, "grouped": 1, "url": ""}}}',
    ],
)
def test_a_file_that_is_not_exactly_the_format_is_rejected_whole(text: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        parse_records(text)
