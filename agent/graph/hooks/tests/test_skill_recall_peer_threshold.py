# meiva:note
# The peer recall threshold reflects the 2026-11-18 recall-first policy over the 999-message
# peer replay (`peer_recall_replay.json`): `PEER_GATE_WORK` screens out pure status and ACK
# messages first (96% recall on work dispatches), and `skill_recall_peer_threshold` (0.45) keeps
# 98.6% recall across the 428 work requests while remaining stricter than the person cut (0.35)
# against residual peer coordination noise. The former 0.70 cut dropped recall to 63.3% (157 of
# 428 work dispatches received no offer). Changing the default re-opens this test on purpose.

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


def test_the_shipped_default_prioritizes_work_dispatch_recall() -> None:
    precision, recall = _precision_and_recall(settings.agent.skill_recall_peer_threshold)

    assert recall >= 0.95, (
        f"coverage fell to {recall:.0%}: recall-first policy must not drop dispatches"
    )
    assert precision >= 0.45, f"unscreened precision fell to {precision:.0%}"


def test_the_person_cut_is_looser_than_the_peer_cut() -> None:
    person_precision, _recall = _precision_and_recall(settings.agent.skill_recall_threshold)
    peer_precision, _recall = _precision_and_recall(settings.agent.skill_recall_peer_threshold)

    assert settings.agent.skill_recall_threshold < settings.agent.skill_recall_peer_threshold
    assert person_precision < peer_precision


def test_the_former_strict_cut_cost_over_a_quarter_of_work_coverage() -> None:
    _precision, recall = _precision_and_recall(settings.agent.skill_recall_peer_threshold)
    _precision_70, recall_70 = _precision_and_recall(0.70)

    assert recall - recall_70 > 0.25, (
        f"0.70 kept {recall_70:.0%} against {recall:.0%}: expected large coverage loss"
    )
