from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from base.events.notifications.catalog import core_catalog
from base.events.notifications.notices import notice_dedupe_key
from cli.commands.agents.notices_audit import (
    MODELLED_CATEGORIES,
    Candidate,
    build_report,
    notice_candidates,
    notice_key,
    parse_simulation,
    per_day,
    per_hour,
    render_json,
    render_text,
    replay,
    window_for,
)

_CATALOG = core_catalog()
_NEEDS_INPUT = "core.needs_input"


def _at(minutes: int) -> datetime:
    return datetime(2026, 10, 1, 12, 0, tzinfo=UTC) + timedelta(minutes=minutes)


def _notice(minutes: int, notice_id: int, question: object = None, *, requestion: bool = True):
    return Candidate(
        _NEEDS_INPUT, _at(minutes), 7, notice_key(notice_id, question, requestion=requestion)
    )


def test_replay_drops_a_repeat_inside_the_dedupe_window() -> None:
    result = replay(
        [_notice(0, 1, {"ask": "a"}), _notice(30, 1, {"ask": "a"})],
        categories=_CATALOG,
        switches={},
    )

    assert len(result.delivered) == 1
    assert result.deduplicated[_NEEDS_INPUT] == 1
    assert result.candidates == 2


def test_replay_delivers_the_same_key_again_an_hour_later() -> None:
    result = replay(
        [_notice(0, 1, {"ask": "a"}), _notice(61, 1, {"ask": "a"})],
        categories=_CATALOG,
        switches={},
    )

    assert len(result.delivered) == 2
    assert result.deduplicated[_NEEDS_INPUT] == 0


def test_replay_never_dedupes_a_null_key() -> None:
    candidates = [Candidate(_NEEDS_INPUT, _at(i), 1, None) for i in (0, 1, 2)]

    result = replay(candidates, categories=_CATALOG, switches={})

    assert len(result.delivered) == 3
    assert result.deduplicated[_NEEDS_INPUT] == 0


def test_requestion_off_swallows_a_changed_question_inside_the_window() -> None:
    first, second = {"prompt": "ship it?"}, {"prompt": "ship it now?"}
    on = replay([_notice(0, 1, first), _notice(5, 1, second)], categories=_CATALOG, switches={})
    off = replay(
        [_notice(0, 1, first, requestion=False), _notice(5, 1, second, requestion=False)],
        categories=_CATALOG,
        switches={},
    )

    assert len(on.delivered) == 2
    assert len(off.delivered) == 1
    assert off.deduplicated[_NEEDS_INPUT] == 1


def test_notice_key_is_the_spine_key_when_requestion_is_on() -> None:
    assert notice_key(9, {"a": 1}, requestion=True) == notice_dedupe_key(9, {"a": 1})
    assert notice_key(9, {"a": 1}, requestion=False) == "notice:9"


def test_per_day_fills_every_day_of_the_window_in_the_requested_zone() -> None:
    start = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    end = start + timedelta(days=2)
    late = datetime(2026, 10, 1, 23, 30, tzinfo=UTC)

    rows = per_day(
        [Candidate(_NEEDS_INPUT, late, 1, "k")], start=start, end=end, tz=ZoneInfo("Asia/Shanghai")
    )

    assert [day.isoformat() for day, _ in rows] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert rows[0][1] == {}
    assert rows[1][1][_NEEDS_INPUT] == 1
    assert rows[2][1] == {}


def test_per_hour_uses_the_requested_zone() -> None:
    moment = datetime(2026, 10, 1, 23, 30, tzinfo=UTC)

    utc = per_hour([Candidate(_NEEDS_INPUT, moment, 1, "k")], tz=UTC)
    shanghai = per_hour([Candidate(_NEEDS_INPUT, moment, 1, "k")], tz=ZoneInfo("Asia/Shanghai"))

    assert utc[23] == 1
    assert shanghai[7] == 1


@pytest.mark.parametrize("spec", ["requestion=maybe", "unknown=on", "requestion"])
def test_parse_simulation_rejects_a_switch_it_does_not_model(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_simulation(spec)


def test_parse_simulation_reads_on_and_off() -> None:
    assert parse_simulation("requestion=on") == ("requestion", True)
    assert parse_simulation("requestion=off") == ("requestion", False)


def _row(*, question: object = None, edited: bool = False) -> list[Candidate]:
    return notice_candidates(
        notice_id=1,
        agent_id=7,
        created_at=_at(0),
        updated_at=_at(30) if edited else _at(0),
        question=question,
        start=_at(-60),
        end=_at(600),
        requestion=True,
    )


def test_a_notice_post_is_one_candidate_keyed_by_its_question() -> None:
    candidates = _row(question={"prompt": "ship it?"})

    assert len(candidates) == 1
    assert candidates[0].dedupe_key == notice_dedupe_key(1, {"prompt": "ship it?"})


def test_a_question_added_after_posting_is_a_second_candidate_with_its_own_key() -> None:
    candidates = _row(question={"prompt": "ship it?"}, edited=True)

    assert [c.at for c in candidates] == [_at(0), _at(30)]
    assert candidates[0].dedupe_key == notice_dedupe_key(1, None)
    assert candidates[1].dedupe_key == notice_dedupe_key(1, {"prompt": "ship it?"})


def test_a_question_added_after_posting_collapses_under_the_simulated_key() -> None:
    candidates = notice_candidates(
        notice_id=1,
        agent_id=7,
        created_at=_at(0),
        updated_at=_at(30),
        question={"prompt": "ship it?"},
        start=_at(-60),
        end=_at(600),
        requestion=False,
    )

    assert [c.dedupe_key for c in candidates] == ["notice:1", "notice:1"]


def test_a_notice_edited_without_a_question_is_not_a_re_ask() -> None:
    assert len(_row(edited=True)) == 1


def test_an_edit_after_the_window_is_not_a_candidate() -> None:
    candidates = notice_candidates(
        notice_id=1,
        agent_id=7,
        created_at=_at(0),
        updated_at=_at(600),
        question={"prompt": "ship it?"},
        start=_at(-60),
        end=_at(600),
        requestion=True,
    )

    assert len(candidates) == 1


def test_the_audit_models_exactly_the_core_catalog() -> None:
    assert set(MODELLED_CATEGORIES) == set(_CATALOG)


def _report(categories: list[str] | None = None):
    names = categories or ["core.needs_input"]
    window = window_for(
        days=2, tz_name="Asia/Shanghai", now=datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
    )
    baseline = replay(
        [_notice(0, 1, {"ask": "a"}), _notice(30, 1, {"ask": "a"})],
        categories=_CATALOG,
        switches={},
    )
    return build_report(
        window=window,
        categories=[_CATALOG[name] for name in names],
        switches={"core.needs_input": True},
        baseline=baseline,
        variants=[("baseline", baseline)],
    )


def test_the_report_carries_every_section_a_consumer_reads() -> None:
    report = _report()

    assert report["window"]["days"] == 2
    assert report["delivered"]["total"] == 1
    assert [row["day"] for row in report["delivered"]["per_day"]] == [
        "2026-09-30",
        "2026-10-01",
        "2026-10-02",
    ]
    assert len(report["delivered"]["per_hour"]) == 24
    assert report["delivered"]["per_category"]["core.needs_input"] == 1
    assert report["candidates"][0]["candidates"] == 2
    assert report["candidates"][0]["suppressed"] == 1
    assert report["simulations"] == [
        {"variant": "baseline", "delivered": 1, "delta": 0, "per_day": 0.5}
    ]
    assert report["sources"]["core.needs_input"].startswith("agent_notices")


def test_json_and_text_report_the_same_totals() -> None:
    report = _report()
    parsed = json.loads(render_json(report))

    assert parsed == report
    assert sum(parsed["delivered"]["per_category"].values()) == parsed["delivered"]["total"]
    text = "\n".join(render_text(parsed, tz=ZoneInfo("Asia/Shanghai")))
    assert "core.needs_input" in text
    assert "Simulations" in text


# meiva:note
# `--explain` answers the question the summary cannot: for one candidate, was it
# delivered, and if not, was it the switch or the dedupe window (and how long the
# agent had waited). The JSON carries the same rows so a report can be built on them.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_explain_names_the_outcome_of_every_candidate() -> None:
    result = replay(
        [_notice(0, 1, {"ask": "a"}), _notice(10, 1, {"ask": "a"}), _notice(120, 1, {"ask": "a"})],
        categories=_CATALOG,
        switches={},
        explain=True,
    )

    outcomes = [item.outcome for item in result.explained]
    assert outcomes[0] == "delivered"
    assert outcomes[1].startswith("duplicate")
    assert outcomes[2] == "delivered"


def test_explain_marks_a_switched_off_category() -> None:
    result = replay(
        [Candidate(_NEEDS_INPUT, _at(0), 3, None)],
        categories=_CATALOG,
        switches={_NEEDS_INPUT: False},
        explain=True,
    )

    assert [item.outcome for item in result.explained] == ["off"]


def test_the_report_carries_the_explanation_and_the_text_lists_it() -> None:
    window = window_for(days=1, tz_name="UTC", now=_at(60 * 24))
    candidates = [_notice(0, 1, {"ask": "a"}), _notice(5, 1, {"ask": "a"})]
    baseline = replay(candidates, categories=_CATALOG, switches={}, explain=True)
    report = build_report(
        window=window,
        categories=[_CATALOG[_NEEDS_INPUT]],
        switches={_NEEDS_INPUT: True},
        baseline=baseline,
        variants=[],
    )

    rows = report["explain"]
    assert [row["outcome"].split()[0] for row in rows] == ["delivered", "duplicate"]
    assert {row["category"] for row in rows} == {_NEEDS_INPUT}
    rendered = "\n".join(render_text(report, tz=window.tz))
    assert "Why each candidate was or was not delivered" in rendered
    assert "duplicate" in rendered
