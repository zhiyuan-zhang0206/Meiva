# meiva:note
# The peer recall threshold is the cut the 2026-10-18 replay selects: 999 real peer messages,
# each with the top skill's probability and whether the message asked the recipient to do work
# (majority of three judges, 86% unanimous). At 0.70 the cut offers on 40% of turns, covers 63%
# of the work requests and 69% of the offers land on one; 0.75 covers 45% at 75%, and 0.60 falls
# to 58% precision. Precision is one rater panel's estimate: an independent reader on 50
# stratified messages put 0.70 at 56%, so the figure to quote is 56-69%. The assertions therefore
# lock the ordering and a floor that both readers clear, not a point estimate. A missed dispatch
# costs a redo while an extra offer costs one line, so coverage wins the tie. Changing the
# default re-opens this test on purpose: move the number together with the rows.

from __future__ import annotations

import json
from pathlib import Path

from base.config import settings

# meiva:note
# The replay rows: one per peer message, the top skill's probability and whether the message
# asked the recipient to do work.
ROWS: tuple[tuple[float, int], ...] = tuple(
    (score, work)
    for score, work in json.loads(Path(__file__).with_name("peer_recall_replay.json").read_text())[
        "rows"
    ]
)


def _precision_and_recall(threshold: float) -> tuple[float, float]:
    offered = [work for score, work in ROWS if score >= threshold]
    hits = sum(offered)
    positives = sum(work for _score, work in ROWS)
    return hits / len(offered), hits / positives


def test_the_replay_fixture_holds_999_messages_and_428_work_requests() -> None:
    assert len(ROWS) == 999
    assert sum(work for _score, work in ROWS) == 428


def test_the_shipped_default_keeps_coverage_without_collapsing_on_precision() -> None:
    precision, recall = _precision_and_recall(settings.agent.skill_recall_peer_threshold)

    assert recall >= 0.60, f"coverage fell to {recall:.0%}: most dispatches would get no offer"
    assert precision >= 0.55, f"precision fell to {precision:.0%}: the note would mostly be noise"


def test_the_person_cut_would_be_noisier_than_the_peer_cut() -> None:
    # meiva:note
    # 0.60 is pinned: this compares the two measured cuts, not whatever the person default becomes.
    person_precision, _recall = _precision_and_recall(0.60)
    peer_precision, _recall = _precision_and_recall(settings.agent.skill_recall_peer_threshold)

    assert person_precision < peer_precision - 0.05


def test_a_higher_cut_costs_a_large_share_of_the_coverage() -> None:
    _precision, recall = _precision_and_recall(settings.agent.skill_recall_peer_threshold)
    _precision_75, recall_75 = _precision_and_recall(0.75)

    assert recall_75 < recall - 0.05, (
        f"0.75 kept {recall_75:.0%} against {recall:.0%}: no trade-off"
    )
