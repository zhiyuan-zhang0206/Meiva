# meiva:note
# The page ledger's pure decisions: who is adopted, who is due, who may be closed.


from __future__ import annotations

import pytest

from services.desktop.browser.tab_groups.grouping import agent_id_of_title, group_title
from services.desktop.browser.tab_groups.page_ledger import (
    PageRecord,
    Reconciled,
    TabState,
    agent_pages,
    closable,
    dump_records,
    parse_records,
    reconcile,
    target_of_url,
)

NOW = 1_000_000.0
TTL = 3_600.0


def _tabs(pages: dict[str, str], groups: dict[str, int] | None = None) -> TabState:
    return TabState(pages=pages, agent_groups=groups or {})


def _reconcile(records: dict[str, PageRecord], tabs: TabState, now: float = NOW) -> Reconciled:
    return reconcile(records, tabs, now=now, default_ttl_seconds=TTL)


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
# ── Reconciliation ──────────────────────────────────────────────────────────


def test_a_record_of_a_target_chrome_lacks_is_dropped() -> None:
    tabs = _tabs({"T1": "https://a/"}, {"T1": 7})
    result = _reconcile({"T1": PageRecord(7, NOW + 10), "T9": PageRecord(7, NOW + 10)}, tabs)
    assert set(result.records) == {"T1"}
    assert result.vanished == ("T9",)
    assert result.to_close == ()


def test_a_grouped_page_without_a_record_is_adopted_with_a_fresh_default_ttl() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://b/"}, {"T1": 7, "T2": 9})
    result = _reconcile({}, tabs)
    assert result.records == {"T1": PageRecord(7, NOW + TTL), "T2": PageRecord(9, NOW + TTL)}
    assert result.adopted == (("T1", 7), ("T2", 9))
    assert result.to_close == ()


def test_a_page_in_no_agent_group_without_a_record_is_not_looked_at() -> None:
    result = _reconcile({}, _tabs({"T1": "https://user/", "T2": "https://work/"}))
    assert result.records == {}
    assert result.adopted == () and result.to_close == () and result.lapsed == ()


def test_the_group_names_the_owner_when_the_record_disagrees() -> None:
    tabs = _tabs({"T1": "https://a/"}, {"T1": 9})
    result = _reconcile({"T1": PageRecord(7, NOW + 50)}, tabs)
    assert result.records == {"T1": PageRecord(9, NOW + 50)}


def test_an_expired_record_in_its_group_is_due_and_stays_recorded_until_closed() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://user/"}, {"T1": 7})
    result = _reconcile({"T1": PageRecord(7, NOW - 1)}, tabs)
    assert [(p.target_id, p.owner, p.url) for p in result.to_close] == [("T1", 7, "https://a/")]
    assert "T1" in result.records


def test_a_record_exactly_at_its_deadline_is_due_and_one_second_before_is_not() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://u/"}, {"T1": 7})
    assert _reconcile({"T1": PageRecord(7, NOW)}, tabs).to_close
    assert not _reconcile({"T1": PageRecord(7, NOW + 1)}, tabs).to_close


def test_an_expired_record_whose_page_is_in_no_agent_group_is_never_closed() -> None:
    tabs = _tabs({"T1": "https://user/", "T2": "https://user2/"})
    result = _reconcile({"T1": PageRecord(7, NOW - 1)}, tabs)
    assert result.to_close == ()
    assert result.lapsed == ("T1",)
    assert result.records == {}


def test_an_unexpired_record_outside_a_group_is_kept_not_closed_and_not_adopted() -> None:
    # meiva:note
    # The group may simply not have been applied yet: keep the record, never close on it.
    tabs = _tabs({"T1": "https://a/", "T2": "https://u/"})
    result = _reconcile({"T1": PageRecord(7, NOW + 100)}, tabs)
    assert result.records == {"T1": PageRecord(7, NOW + 100)}
    assert result.to_close == () and result.lapsed == ()


def test_a_deadline_beyond_one_grant_ahead_is_pulled_back_after_a_clock_step_back() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://u/"}, {"T1": 7})
    result = _reconcile({"T1": PageRecord(7, NOW + 10 * 86_400)}, tabs)
    assert result.records["T1"].expires_at == NOW + 86_400
    assert result.to_close == ()


def test_a_configured_ttl_longer_than_a_day_keeps_its_own_ceiling() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://u/"}, {"T1": 7})
    result = reconcile({}, tabs, now=NOW, default_ttl_seconds=3 * 86_400)
    assert result.records["T1"].expires_at == NOW + 3 * 86_400


def test_a_stepped_back_clock_never_makes_a_record_due() -> None:
    tabs = _tabs({"T1": "https://a/", "T2": "https://u/"}, {"T1": 7})
    for now in (NOW - 1, NOW - 86_400, NOW - 10**7):
        assert _reconcile({"T1": PageRecord(7, NOW + 5)}, tabs, now=now).to_close == ()


# meiva:note
# ── What may be closed ──────────────────────────────────────────────────────


def test_closable_requires_an_agent_group_and_leaves_one_page_open() -> None:
    tabs = _tabs({"T1": "u1", "T2": "u2", "T3": "u3"}, {"T1": 7, "T2": 7})
    assert closable(["T1", "T2", "T3", "T9"], tabs) == ["T1", "T2"]
    only_ours = _tabs({"T1": "u1", "T2": "u2"}, {"T1": 7, "T2": 7})
    assert len(closable(["T1", "T2"], only_ours)) == 1


def test_agent_pages_are_the_agents_recorded_pages_in_the_agents_own_group() -> None:
    tabs = _tabs(
        {"T1": "a", "T2": "b", "T3": "c", "T4": "d", "T5": "blank"},
        {"T1": 7, "T2": 7, "T3": 8},
    )
    records = {
        "T1": PageRecord(7, NOW),
        "T2": PageRecord(7, NOW),
        # meiva:note
        # now in agent 8's group: not 7's to close
        "T3": PageRecord(7, NOW),
        # meiva:note
        # in no group
        "T4": PageRecord(7, NOW),
        # meiva:note
        # not in Chrome
        "T9": PageRecord(7, NOW),
    }
    assert [p.target_id for p in agent_pages(records, tabs, 7)] == ["T1", "T2"]
    assert agent_pages(records, tabs, 99) == []


def test_target_of_url_needs_exactly_one_match_among_the_agents_own_pages() -> None:
    tabs = _tabs(
        {"T1": "https://a/", "T2": "https://a/", "T3": "https://b/", "T4": "https://c/"},
        {"T1": 7, "T2": 7, "T3": 7, "T4": 8},
    )
    records = {t: PageRecord(7, NOW) for t in ("T1", "T2", "T3", "T4")}
    assert target_of_url(records, tabs, 7, "https://b/") == "T3"
    # meiva:note
    # ambiguous
    assert target_of_url(records, tabs, 7, "https://a/") is None
    # meiva:note
    # owned by another agent
    assert target_of_url(records, tabs, 7, "https://c/") is None
    assert target_of_url(records, tabs, 7, "https://zzz/") is None


# meiva:note
# ── The file format ─────────────────────────────────────────────────────────


def test_records_round_trip_through_the_file_format() -> None:
    records = {"T2": PageRecord(8, 12.5), "T1": PageRecord(7, 1_700_000_000.25)}
    assert parse_records(dump_records(records)) == records
    assert parse_records(dump_records({})) == {}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "null",
        "[]",
        '{"pages": {}}',
        '{"version": 2, "pages": {}}',
        '{"version": 1, "pages": []}',
        '{"version": 1, "pages": {"T1": []}}',
        '{"version": 1, "pages": {"T1": {"owner": 7}}}',
        '{"version": 1, "pages": {"T1": {"owner": 7.5, "expires_at": 1}}}',
        '{"version": 1, "pages": {"T1": {"owner": 7, "expires_at": Infinity}}}',
        '{"version": 1, "pages": {"T1": {"owner": 7, "expires_at": null}}}',
    ],
)
def test_a_file_that_is_not_exactly_the_format_is_rejected_whole(text: str) -> None:
    with pytest.raises(ValueError):
        parse_records(text)
