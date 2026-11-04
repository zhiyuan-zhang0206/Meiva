# meiva:note
# The slot check: a six-hourly cron that stops arriving leaves nothing to read, so "no red" and "no
# run" look the same from the red watch. These cases pin the one boundary that matters (is the slot
# really late?), the suppression while a run is in flight, and the rule that one missing slot
# produces one make-up dispatch however many rounds notice it.

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from schedules.plans.watch.release_gate_slot import (
    RELEASE_GATE_SLOT_STALE_AFTER,
    SlotFacts,
    decide_slot,
)

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)


def _facts(hours_ago: float | None, *, busy: bool = False) -> SlotFacts:
    start = None if hours_ago is None else NOW - timedelta(hours=hours_ago)
    return SlotFacts(last_schedule_start=start, busy=busy)


def _decide(hours_ago: float | None, *, busy: bool = False, uncertified: int | None = 3):
    facts = replace(_facts(hours_ago, busy=busy), uncertified_commits=uncertified)
    return decide_slot(facts, now=NOW, since=None, stale_after=RELEASE_GATE_SLOT_STALE_AFTER)


def test_a_late_arrival_is_not_a_missing_slot() -> None:
    decision = _decide(8.0)

    assert decision.card is None
    assert decision.gap_id == (NOW - timedelta(hours=8)).isoformat(timespec="seconds")


def test_a_slot_past_the_band_opens_a_card_that_publishes_nothing() -> None:
    decision = _decide(13.5)

    assert decision.card is not None
    assert decision.card.fingerprint == "main-red-watch/release-gate-slot"
    assert "13.5h" in decision.card.title
    assert decision.card.body.startswith("**This card publishes nothing.**")
    assert "gh workflow run main-typecheck.yml" in decision.card.body
    assert "type surface only" in decision.card.body
    assert "gh workflow run release-gate.yml" in decision.card.body
    assert decision.note is not None


def test_a_slot_nothing_was_waiting_on_opens_no_card() -> None:
    decision = _decide(13.5, uncertified=0)

    assert decision.card is None
    assert decision.note is not None and "nothing uncertified" in decision.note


def test_an_unreadable_commit_distance_still_opens_the_card() -> None:
    decision = _decide(13.5, uncertified=None)

    assert decision.card is not None
    assert "could not be read" in decision.card.body


def test_the_gap_keeps_its_name_across_rounds() -> None:
    facts = replace(_facts(13.5), uncertified_commits=3)

    first = decide_slot(facts, now=NOW, since=None, stale_after=RELEASE_GATE_SLOT_STALE_AFTER)
    second = decide_slot(
        facts,
        now=NOW + timedelta(minutes=30),
        since=None,
        stale_after=RELEASE_GATE_SLOT_STALE_AFTER,
    )

    assert first.gap_id == second.gap_id, "the same missing slot names the same gap"


def test_a_running_workflow_is_not_a_missing_slot() -> None:
    decision = _decide(13.5, busy=True)

    assert decision.card is None


def test_a_newer_scheduled_run_clears_the_gap() -> None:
    decision = _decide(0.5)

    assert decision.card is None


def test_no_run_on_record_waits_out_one_threshold_from_the_first_round() -> None:
    young = decide_slot(
        SlotFacts(None, busy=False, uncertified_commits=2),
        now=NOW,
        since=NOW - timedelta(hours=1),
        stale_after=RELEASE_GATE_SLOT_STALE_AFTER,
    )
    old = decide_slot(
        SlotFacts(None, busy=False, uncertified_commits=2),
        now=NOW,
        since=NOW - timedelta(hours=13),
        stale_after=RELEASE_GATE_SLOT_STALE_AFTER,
    )

    assert young.card is None
    assert old.card is not None


# meiva:note
# The threshold has to be replayed against the gaps that were actually observed, not argued about.
# Five consecutive release-gate gaps, of which two really missed a slot: a threshold that opens a
# card anywhere else than on those two reports lateness as a missing slot.
def test_the_measured_gaps_open_only_the_windows_that_missed_a_slot() -> None:
    gaps = (4 + 9 / 60, 8 + 9 / 60, 9 + 59 / 60, 8 + 49 / 60, 13 + 21 / 60)

    opened = tuple(_decide(gap).card is not None for gap in gaps)

    assert opened == (False, False, True, False, True)
