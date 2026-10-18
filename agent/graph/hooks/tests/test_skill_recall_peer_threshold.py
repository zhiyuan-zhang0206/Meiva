"""The peer recall threshold is the cut the 2026-10-18 replay selects.

`peer_recall_replay.ROWS` is that replay: one row per real peer message, holding the top
skill's probability of "yes" and whether the message actually asked the recipient to do work
(majority of three judges, 86% unanimous). The default is a measurement, not a taste: at 0.70
the cut offers on 40% of turns, covers 63% of the work requests and 69% of the offers land on
one; 0.75 covers only 45% at 75%, and 0.60 falls to 58% precision. A missed dispatch costs a
redo while an extra offer costs one line, so coverage wins the tie.

Changing the default re-opens this test on purpose: if a new replay supports a different cut,
move the number together with the rows.
"""

from __future__ import annotations

from agent.graph.hooks.tests.peer_recall_replay import ROWS
from base.config import settings


def _precision_and_recall(threshold: float) -> tuple[float, float]:
    offered = [work for score, work in ROWS if score >= threshold]
    hits = sum(offered)
    positives = sum(work for _score, work in ROWS)
    return hits / len(offered), hits / positives


def test_the_replay_fixture_covers_a_full_week_of_peer_traffic() -> None:
    assert len(ROWS) == 999
    assert sum(work for _score, work in ROWS) == 428


def test_the_shipped_default_offers_on_most_work_requests() -> None:
    precision, recall = _precision_and_recall(settings.agent.skill_recall_peer_threshold)

    assert recall >= 0.60, f"coverage fell to {recall:.0%}: most dispatches would get no offer"
    assert precision >= 0.60, f"precision fell to {precision:.0%}: the note would mostly be noise"


def test_the_person_cut_would_be_too_noisy_on_peer_traffic() -> None:
    precision, _recall = _precision_and_recall(settings.agent.skill_recall_threshold)

    assert precision < 0.60


def test_one_cut_higher_loses_a_third_of_the_coverage() -> None:
    _precision, recall = _precision_and_recall(0.75)

    assert recall < 0.50
