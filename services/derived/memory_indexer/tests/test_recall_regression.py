# meiva:note
# The evaluator is pure, so the fixture and the held/slipped/lost rule are tested without a
# gateway, an index or a model; the live replay that feeds it a results file is documented in
# the component's docs node.
# meiva:link services/docs/gateway_side/memory_indexer/memory-recall-regression.meiva.okf.md

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.derived.memory_indexer.recall_regression import (
    CASES_PATH,
    Case,
    evaluate,
    load_cases,
    main,
    report,
)

THRESHOLD = 0.72


def _case(rank: int = 1, score: float = 0.74) -> Case:
    return Case(
        id="recall-99",
        gloss="gloss",
        language="zh",
        shape="nl",
        message="message",
        path="topic/note.md",
        baseline_rank=rank,
        baseline_score=score,
    )


def test_fixture_cases_are_well_formed() -> None:
    cases = load_cases()

    assert len(cases) == 30
    assert len({case.id for case in cases}) == 30
    assert all(case.message.strip() for case in cases)
    assert all(case.gloss.strip() for case in cases)
    assert all(case.gloss.isascii() for case in cases)
    assert all(case.path.endswith(".md") for case in cases)
    assert all(1 <= case.baseline_rank <= 5 for case in cases)
    assert all(0.0 <= case.baseline_score < 0.80 for case in cases), "cases are the blocked ones"
    assert CASES_PATH.is_file()


def test_fixture_stores_messages_as_ascii_escapes() -> None:
    assert CASES_PATH.read_bytes().isascii(), (
        "the repo is English-only; messages stay \\uXXXX-escaped"
    )


def test_a_case_that_keeps_its_rank_holds() -> None:
    outcome = evaluate(_case(rank=2), [("other.md", 0.9), ("topic/note.md", 0.71)])

    assert (outcome.rank, outcome.score, outcome.verdict) == (2, 0.71, "held")


def test_a_case_that_ranks_lower_slips() -> None:
    outcome = evaluate(_case(rank=1), [("a.md", 0.9), ("b.md", 0.8), ("topic/note.md", 0.7)])

    assert (outcome.rank, outcome.verdict) == (3, "slipped")


def test_a_case_whose_note_is_gone_is_lost() -> None:
    outcome = evaluate(_case(rank=1), [("a.md", 0.9), ("b.md", 0.8)])

    assert (outcome.rank, outcome.score, outcome.verdict) == (None, None, "lost")


def test_delivery_needs_the_top_three_and_the_threshold() -> None:
    assert evaluate(_case(), [("topic/note.md", 0.72)]).delivered(THRESHOLD)
    assert not evaluate(_case(), [("topic/note.md", 0.71)]).delivered(THRESHOLD)
    fourth = [("a.md", 0.9), ("b.md", 0.9), ("c.md", 0.9), ("topic/note.md", 0.9)]
    assert not evaluate(_case(), fourth).delivered(THRESHOLD)


def test_report_names_every_case_that_moved() -> None:
    held = evaluate(_case(), [("topic/note.md", 0.75)])
    lost = evaluate(_case(rank=2), [("a.md", 0.9)])
    slipped_case = Case("recall-98", "gloss", "en", "path", "m", "other/note.md", 1, 0.77)
    slipped = evaluate(slipped_case, [("a.md", 0.9), ("other/note.md", 0.60)])

    text = report([held, lost, slipped], threshold=THRESHOLD)

    assert "held        1" in text
    assert "slipped     1" in text
    assert "lost        1" in text
    assert "recall-99: topic/note.md no longer returned" in text
    assert "recall-98: other/note.md rank 1 -> 2, score 0.77 -> 0.60" in text


def test_cli_reports_the_baseline_itself_as_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = load_cases()
    results = {case.id: [{"path": case.path, "score": case.baseline_score}] for case in cases}
    payload = tmp_path / "results.json"
    payload.write_text(json.dumps(results), encoding="utf-8")

    assert main(["--results", str(payload)]) == 0

    out = capsys.readouterr().out
    assert "30 cases" in out
    assert "held       30" in out
    assert "lost        0" in out


def test_cli_refuses_results_that_skip_a_case(tmp_path: Path) -> None:
    payload = tmp_path / "results.json"
    payload.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="missing"):
        main(["--results", str(payload)])
