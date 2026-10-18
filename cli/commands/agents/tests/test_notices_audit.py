from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from base.events.notifications.catalog import core_catalog
from base.events.notifications.notices import notice_dedupe_key
from cli.commands.agents.notices_audit import (
    MODELLED_CATEGORIES,
    Candidate,
    notice_candidates,
    notice_key,
    parse_simulation,
    per_day,
    per_hour,
    replay,
)

_CATALOG = core_catalog()
_NEEDS_INPUT = "core.needs_input"
_TURN_FINISHED = "core.turn_finished"


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


def test_replay_reports_a_switched_off_turn_finished_without_delivering_it() -> None:
    candidates = [Candidate(_TURN_FINISHED, _at(i), i, None) for i in range(5)]

    off = replay(candidates, categories=_CATALOG, switches={})
    on = replay(candidates, categories=_CATALOG, switches={_TURN_FINISHED: True})

    assert len(off.delivered) == 0
    assert off.switched_off[_TURN_FINISHED] == 5
    assert len(on.delivered) == 5


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


@pytest.mark.parametrize("spec", ["turn_finished=maybe", "unknown=on", "turn_finished"])
def test_parse_simulation_rejects_a_switch_it_does_not_model(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_simulation(spec)


def test_parse_simulation_reads_on_and_off() -> None:
    assert parse_simulation("turn_finished=on") == ("turn_finished", True)
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


def test_modelled_categories_each_have_a_producer_source() -> None:
    assert set(MODELLED_CATEGORIES) == set(_CATALOG)
