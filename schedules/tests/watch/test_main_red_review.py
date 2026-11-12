from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schedules.jobs.watch.main_red_watch import (
    STATE_FILE,
    WatchState,
    load_state,
    save_state,
    watch_once,
)
from schedules.plans.watch.junit_cases import CaseObservation, CaseOutcome
from schedules.plans.watch.main_red import Card, Entry, Failure, RunFacts, STATE_CONFIRMED

FIRST = datetime(2026, 11, 10, 23, 1, tzinfo=UTC)
CASE = "tests/integration/test_schedule_runner_cleanup.py::test_runner_hard_exit_child_ownership[complete]"


def red() -> RunFacts:
    return RunFacts(
        "ci.yml",
        "red-sha",
        FIRST,
        "failure",
        (Failure(CASE, "test", job="backend shard 8/8"),),
        run_id=10,
    )


def round_once(
    tmp_path: Path,
    runs: tuple[RunFacts, ...],
    outcome: CaseOutcome,
    cards: list[Card],
    closed: list[int],
    events: list[Mapping[str, object]],
) -> None:
    def open_card(card: Card) -> int:
        cards.append(card)
        return len(cards) + 100

    watch_once(
        state_path=tmp_path / STATE_FILE,
        now=FIRST + timedelta(days=1),
        fetch=lambda _workflow, _when, _attempt: runs,
        last_start=lambda _workflow: FIRST,
        open_card=open_card,
        close_card=lambda task_id, _why: closed.append(task_id),
        emit=events.append,
        workflows=("ci.yml",),
        liveness=(),
        notify=None,
        read_case=lambda _entry, _run: CaseObservation(outcome, "artifact exact case evidence"),
    )


@pytest.mark.parametrize("outcome", [case for case in CaseOutcome if case != CaseOutcome.FAILED])
def test_first_opportunity_is_classified_and_cards_once(
    tmp_path: Path, outcome: CaseOutcome
) -> None:
    cards: list[Card] = []
    closed: list[int] = []
    events: list[Mapping[str, object]] = []
    round_once(tmp_path, (red(),), outcome, cards, closed, events)
    assert cards == []
    assert load_state(tmp_path / STATE_FILE).entries["ci.yml::" + CASE].opportunities == 0
    later = RunFacts("ci.yml", "new-sha", FIRST + timedelta(hours=8), "failure", (), run_id=11)
    round_once(tmp_path, (later, red()), outcome, cards, closed, events)
    assert len(cards) == 1
    assert CASE in cards[0].body
    assert "opportunities: 1" in cards[0].body
    assert outcome.value in cards[0].body
    entry = load_state(tmp_path / STATE_FILE).entries["ci.yml::" + CASE]
    assert entry.review_outcome == outcome
    assert entry.review_run_id == 11
    assert entry.opportunities == 1
    assert entry.task_id == 101
    assert events[-1]["suspects"]
    round_once(tmp_path, (later, red()), outcome, cards, closed, events)
    assert len(cards) == 1
    assert load_state(tmp_path / STATE_FILE).entries[entry.fingerprint].opportunities == 1


def test_workflow_green_with_skip_keeps_case_open_then_exact_green_closes(tmp_path: Path) -> None:
    cards: list[Card] = []
    closed: list[int] = []
    events: list[Mapping[str, object]] = []
    round_once(tmp_path, (red(),), CaseOutcome.SKIPPED, cards, closed, events)
    later = RunFacts("ci.yml", "new-sha", FIRST + timedelta(hours=8), "success", (), run_id=11)
    round_once(tmp_path, (later,), CaseOutcome.SKIPPED, cards, closed, events)
    assert len(cards) == 1 and not closed
    rerun = replace(later, attempt=2)
    round_once(tmp_path, (rerun,), CaseOutcome.SKIPPED, cards, closed, events)
    assert load_state(tmp_path / STATE_FILE).entries["ci.yml::" + CASE].opportunities == 1
    green = replace(later, run_id=12, created_at=FIRST + timedelta(hours=9))
    round_once(tmp_path, (green,), CaseOutcome.PASSED, cards, closed, events)
    assert closed == [101]
    assert not load_state(tmp_path / STATE_FILE).entries


def test_second_red_uses_normal_confirmation_without_opportunity_card(tmp_path: Path) -> None:
    cards: list[Card] = []
    closed: list[int] = []
    events: list[Mapping[str, object]] = []
    round_once(tmp_path, (red(),), CaseOutcome.PASSED, cards, closed, events)
    later = replace(red(), run_id=11, created_at=FIRST + timedelta(hours=8))
    round_once(tmp_path, (later,), CaseOutcome.FAILED, cards, closed, events)
    assert len(cards) == 1
    assert "Single main red re-check" not in cards[0].title
    assert load_state(tmp_path / STATE_FILE).entries["ci.yml::" + CASE].state == STATE_CONFIRMED


def test_junit_second_red_confirms_and_existing_review_card_is_reused(tmp_path: Path) -> None:
    cards: list[Card] = []
    closed: list[int] = []
    events: list[Mapping[str, object]] = []
    round_once(tmp_path, (red(),), CaseOutcome.UNREADABLE, cards, closed, events)
    later = RunFacts("ci.yml", "new", FIRST + timedelta(hours=8), "failure", (), run_id=11)
    round_once(tmp_path, (later,), CaseOutcome.SKIPPED, cards, closed, events)
    assert len(cards) == 1
    second_red = replace(red(), run_id=12, created_at=FIRST + timedelta(hours=9))
    round_once(tmp_path, (second_red,), CaseOutcome.FAILED, cards, closed, events)
    assert len(cards) == 1
    assert load_state(tmp_path / STATE_FILE).entries["ci.yml::" + CASE].red_count == 2
    green = replace(later, run_id=13, created_at=FIRST + timedelta(hours=10))
    round_once(tmp_path, (green,), CaseOutcome.PASSED, cards, closed, events)
    assert closed == [101]


def test_case_failed_in_junit_is_a_second_red_not_an_opportunity_card(tmp_path: Path) -> None:
    cards: list[Card] = []
    closed: list[int] = []
    events: list[Mapping[str, object]] = []
    round_once(tmp_path, (red(),), CaseOutcome.UNREADABLE, cards, closed, events)
    later = RunFacts("ci.yml", "new", FIRST + timedelta(hours=8), "failure", (), run_id=11)
    round_once(tmp_path, (later,), CaseOutcome.FAILED, cards, closed, events)
    assert len(cards) == 1
    assert "Single main red re-check" not in cards[0].title
    assert load_state(tmp_path / STATE_FILE).entries["ci.yml::" + CASE].state == STATE_CONFIRMED


def test_phase_one_opportunity_is_backfilled_without_charging_or_replaying_runs(
    tmp_path: Path,
) -> None:
    cards: list[Card] = []
    closed: list[int] = []
    events: list[Mapping[str, object]] = []
    round_once(tmp_path, (red(),), CaseOutcome.UNREADABLE, cards, closed, events)
    state = load_state(tmp_path / STATE_FILE)
    name = "ci.yml::" + CASE
    later = RunFacts("ci.yml", "new", FIRST + timedelta(hours=8), "failure", (), run_id=11)
    unseen = replace(later, run_id=12, created_at=FIRST + timedelta(hours=9))
    save_state(
        tmp_path / STATE_FILE,
        replace(
            state,
            entries={name: replace(state.entries[name], opportunities=1)},
            watermarks={"ci.yml": later.created_at.isoformat() + "|1"},
        ),
    )
    reads: list[int] = []

    def read_case(_entry: object, run: RunFacts) -> CaseObservation:
        reads.append(run.run_id)
        return CaseObservation(CaseOutcome.PASSED, "real case in shard 3")

    def open_card(card: Card) -> int:
        cards.append(card)
        return 101

    for _ in range(2):
        watch_once(
            state_path=tmp_path / STATE_FILE,
            now=FIRST + timedelta(days=1),
            fetch=lambda _workflow, _when, _attempt: (),
            last_start=lambda _: FIRST,
            open_card=open_card,
            close_card=lambda i, _: closed.append(i),
            emit=events.append,
            workflows=("ci.yml",),
            liveness=(),
            notify=None,
            read_case=read_case,
            review_history=lambda _: (unseen, later, red()),
        )
    assert reads == [11]
    assert len(cards) == 1
    entry = load_state(tmp_path / STATE_FILE).entries[name]
    assert entry.opportunities == 1
    assert entry.review_run_id == 11 and entry.review_outcome == CaseOutcome.PASSED


@pytest.mark.parametrize("unreadable", [False, True])
def test_unavailable_phase_one_history_still_exposes_one_review_card(
    tmp_path: Path, unreadable: bool
) -> None:

    name = "ci.yml::" + CASE
    entry = Entry(name, "suspected", "red", FIRST, "red", FIRST, 1, opportunities=1, run_id=10)
    later = RunFacts("ci.yml", "new", FIRST + timedelta(hours=8), "failure", (), run_id=11)
    save_state(
        tmp_path / STATE_FILE,
        WatchState({name: entry}, {"ci.yml": later.created_at.isoformat() + "|1"}, {}),
    )
    cards: list[Card] = []

    def history(_workflow: str) -> tuple[RunFacts, ...]:
        if unreadable:
            raise OSError("artifact service unavailable")
        return ()

    def open_card(card: Card) -> int:
        cards.append(card)
        return 101

    for _ in range(2):
        watch_once(
            state_path=tmp_path / STATE_FILE,
            now=FIRST + timedelta(days=1),
            fetch=lambda _workflow, _when, _attempt: (),
            last_start=lambda _: FIRST,
            open_card=open_card,
            close_card=lambda _id, _why: None,
            emit=lambda _payload: None,
            workflows=("ci.yml",),
            liveness=(),
            notify=None,
            read_case=lambda _entry, _run: CaseObservation(CaseOutcome.PASSED, "unused"),
            review_history=history,
        )
    assert len(cards) == 1
    state = load_state(tmp_path / STATE_FILE)
    assert state.entries[name].opportunities == 1
    assert state.entries[name].review_outcome == CaseOutcome.UNREADABLE
    assert "opportunities: 1" in cards[0].body
    assert "unreadable" in cards[0].body
