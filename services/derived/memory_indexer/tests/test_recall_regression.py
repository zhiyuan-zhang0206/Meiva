# meiva:note
# The evaluator is pure, so the fixture and the two verdict families are tested without a
# gateway, an index or a model. One test drives the whole fixture through both halves at two
# thresholds: that is the property the set exists for, and it fails if either half loses its
# direction.
# meiva:link services/docs/gateway_side/memory_indexer/memory-recall-regression.meiva.okf.md

from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.config import settings
from services.derived.memory_indexer.recall_regression import (
    CASES_PATH,
    PRECISION,
    RECALL,
    Case,
    evaluate,
    evaluate_precision,
    injection_policy,
    load_cases,
    main,
    report,
)

THRESHOLD = 0.72
CAP = 3


def _case(group: str = RECALL, rank: int | None = 1, path: str | None = "topic/note.md") -> Case:
    return Case(
        id="recall-99",
        group=group,
        gloss="gloss",
        language="zh",
        shape="nl",
        message="message",
        path=path,
        baseline_rank=rank,
        baseline_score=0.74,
        baseline_delivered=False,
    )


def test_fixture_has_both_directions() -> None:
    cases = load_cases()
    groups = [case.group for case in cases]

    assert groups.count(RECALL) == 30
    assert groups.count(PRECISION) == 25
    assert len({case.id for case in cases}) == 55
    assert all(case.message.strip() and case.gloss.strip() for case in cases)
    assert all(case.gloss.isascii() for case in cases)
    assert CASES_PATH.read_bytes().isascii(), (
        "the repo is English-only; messages stay \\uXXXX-escaped"
    )


def test_recall_cases_sit_below_the_old_threshold_and_precision_cases_above_the_new_one() -> None:
    cases = load_cases()

    assert all(case.baseline_score < 0.80 for case in cases if case.group == RECALL)
    firing = [case for case in cases if case.group == PRECISION and case.baseline_delivered]
    assert len(firing) == 15, "the set records what the shipped default actually did"
    assert all(case.baseline_score >= THRESHOLD for case in firing)


def test_injection_policy_reads_the_agent_config() -> None:
    threshold, cap = injection_policy()

    assert threshold == settings.agent.memory_recall_inject_threshold
    assert cap == settings.agent.memory_recall_inject_max


def test_a_recall_case_that_keeps_its_rank_holds() -> None:
    outcome = evaluate(
        _case(rank=2), [("other.md", 0.9), ("topic/note.md", 0.71)], threshold=THRESHOLD, cap=CAP
    )

    assert (outcome.rank, outcome.score, outcome.verdict) == (2, 0.71, "held")


def test_a_recall_case_that_ranks_lower_slips() -> None:
    outcome = evaluate(
        _case(rank=1),
        [("a.md", 0.9), ("b.md", 0.8), ("topic/note.md", 0.7)],
        threshold=THRESHOLD,
        cap=CAP,
    )

    assert (outcome.rank, outcome.verdict) == (3, "slipped")


def test_a_recall_case_whose_note_is_gone_is_lost() -> None:
    outcome = evaluate(_case(rank=1), [("a.md", 0.9), ("b.md", 0.8)], threshold=THRESHOLD, cap=CAP)

    assert (outcome.rank, outcome.score, outcome.verdict) == (None, None, "lost")


def test_delivery_needs_the_top_three_and_the_threshold() -> None:
    assert evaluate(_case(), [("topic/note.md", 0.72)], threshold=THRESHOLD, cap=CAP).delivered
    assert not evaluate(_case(), [("topic/note.md", 0.71)], threshold=THRESHOLD, cap=CAP).delivered
    fourth = [("a.md", 0.9), ("b.md", 0.9), ("c.md", 0.9), ("topic/note.md", 0.9)]
    assert not evaluate(_case(), fourth, threshold=THRESHOLD, cap=CAP).delivered


def test_a_precision_case_fires_on_any_note_above_the_threshold() -> None:
    case = _case(group=PRECISION, rank=None, path=None)

    assert (
        evaluate_precision(case, [("noise.md", 0.76)], threshold=THRESHOLD, cap=CAP).verdict
        == "fired"
    )
    assert (
        evaluate_precision(case, [("noise.md", 0.71)], threshold=THRESHOLD, cap=CAP).verdict
        == "silent"
    )
    assert evaluate_precision(case, [], threshold=THRESHOLD, cap=CAP).verdict == "silent"


def test_a_precision_case_reaches_past_the_first_match_but_not_past_the_cap() -> None:
    case = _case(group=PRECISION, rank=None, path=None)
    buried = [("first.md", 0.40), ("second.md", 0.73)]

    outcome = evaluate_precision(case, buried, threshold=THRESHOLD, cap=CAP)

    assert (outcome.rank, outcome.score, outcome.verdict) == (2, 0.73, "fired")
    assert (
        evaluate_precision(
            case,
            [("a.md", 0.40), ("b.md", 0.40), ("c.md", 0.40), buried[1]],
            threshold=THRESHOLD,
            cap=CAP,
        ).verdict
        == "silent"
    )
    assert evaluate_precision(case, buried, threshold=THRESHOLD, cap=1).verdict == "silent"


def test_report_counts_both_halves_and_names_movements() -> None:
    held = evaluate(_case(), [("topic/note.md", 0.75)], threshold=THRESHOLD, cap=CAP)
    lost = evaluate(_case(rank=2), [("a.md", 0.9)], threshold=THRESHOLD, cap=CAP)
    slipped_case = Case(
        "recall-98", RECALL, "gloss", "en", "path", "m", "other/note.md", 1, 0.77, False
    )
    slipped = evaluate(
        slipped_case, [("a.md", 0.9), ("other/note.md", 0.60)], threshold=THRESHOLD, cap=CAP
    )
    quiet = _case(group=PRECISION, rank=None, path=None)
    fired = evaluate_precision(quiet, [("noise.md", 0.80)], threshold=THRESHOLD, cap=CAP)

    text = report([held, lost, slipped, fired], threshold=THRESHOLD, cap=CAP)

    assert "recall set (a usable note existed): 3 cases" in text
    assert "precision set (nothing usable existed): 1 cases" in text
    assert "recall-99: topic/note.md no longer returned" in text
    assert "recall-98: other/note.md rank 1 -> 2, score 0.77 -> 0.60" in text
    assert "newly firing recall-99" in text


def test_lowering_the_threshold_moves_the_two_halves_against_each_other() -> None:
    cases = load_cases()
    results = {
        case.id: (
            [{"path": case.path, "score": case.baseline_score}]
            if case.group == RECALL and case.path
            else [{"path": "noise.md", "score": case.baseline_score}]
        )
        for case in cases
    }

    def counts(threshold: float) -> tuple[int, int]:
        delivered = sum(
            1
            for case in cases
            if case.group == RECALL
            and evaluate(
                case,
                [(str(row["path"]), float(row["score"])) for row in results[case.id]],
                threshold=threshold,
                cap=CAP,
            ).delivered
        )
        fired = sum(
            1
            for case in cases
            if case.group == PRECISION
            and evaluate_precision(
                case,
                [(str(row["path"]), float(row["score"])) for row in results[case.id]],
                threshold=threshold,
                cap=CAP,
            ).verdict
            == "fired"
        )
        return delivered, fired

    strict = counts(0.80)
    shipped = counts(0.72)
    one_step = counts(0.65)
    loose = counts(0.50)

    assert strict[0] < shipped[0] < loose[0], "the recall half must improve as the threshold drops"
    assert shipped[0] < one_step[0], "the next step down must move the recall half too"
    assert strict[1] <= shipped[1] < one_step[1] < loose[1], (
        "the precision half must degrade at every step, not only at the far end"
    )


def test_cli_reports_the_baseline_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = load_cases()
    payload = tmp_path / "results.json"
    payload.write_text(
        json.dumps(
            {
                case.id: [{"path": case.path or "noise.md", "score": case.baseline_score}]
                for case in cases
            }
        ),
        encoding="utf-8",
    )

    assert main(["--results", str(payload), "--threshold", "0.72", "--cap", "3"]) == 0

    out = capsys.readouterr().out
    assert "55 cases" in out
    assert "delivered  20" in out
    assert "fired  15" in out


def test_cli_refuses_results_that_skip_a_case(tmp_path: Path) -> None:
    payload = tmp_path / "results.json"
    payload.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="missing"):
        main(["--results", str(payload)])
