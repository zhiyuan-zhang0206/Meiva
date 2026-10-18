from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from meiva_builtins.skills.tests.skill_script import load_skill_script

cal = cast(Any, load_skill_script("platform", "meiva-self-evolution", "scripts", "calibrate.py"))
CASES = Path(__file__).parent / "fixtures" / "eval_calibration" / "cases.json"


def test_fixture_is_balanced_and_readable() -> None:
    cases = cal.load_cases(CASES)
    assert len(cases) == 24
    assert sum(1 for c in cases if c.expected == "pass") == 12
    assert sum(1 for c in cases if c.expected == "fail") == 12
    raw = json.loads(CASES.read_text(encoding="utf-8"))
    assert all(item["task"] and item["evidence"] for item in raw)


def test_always_pass_only_matches_its_baseline() -> None:
    cases = cal.load_cases(CASES)
    report = cal.calibrate(cases, cal.stub_judge("always-pass"))
    assert report.accuracy == report.always_pass_accuracy == 0.5
    assert report.sensitivity == 0.0
    assert report.beats_baselines is False


def test_abstaining_never_earns_accuracy() -> None:
    cases = cal.load_cases(CASES)
    report = cal.calibrate(cases, cal.stub_judge("always-uncertain"))
    assert report.accuracy == 0.0
    assert report.abstain_rate == 1.0
    assert report.beats_baselines is False


def test_inverted_judge_scores_zero() -> None:
    cases = cal.load_cases(CASES)
    report = cal.calibrate(cases, cal.stub_judge("inverted"))
    assert report.accuracy == 0.0
    assert report.sensitivity == 0.0
    assert report.specificity == 0.0


def test_perfect_judge_beats_both_baselines() -> None:
    cases = cal.load_cases(CASES)
    report = cal.calibrate(cases, cal.stub_judge("perfect"))
    assert report.accuracy == report.sensitivity == report.specificity == 1.0
    assert report.beats_baselines is True


def test_one_sided_set_is_rejected() -> None:
    cases = [c for c in cal.load_cases(CASES) if c.expected == "pass"]
    with pytest.raises(ValueError, match="both expected-pass and expected-fail"):
        cal.calibrate(cases, cal.stub_judge("perfect"))


def test_unknown_verdict_is_rejected() -> None:
    cases = cal.load_cases(CASES)

    def bad_judge(_case: Any) -> str:
        return "maybe"

    with pytest.raises(ValueError, match="expected one of"):
        cal.calibrate(cases, bad_judge)


def test_synthesize_failure_flips_the_outcome() -> None:
    passing = cal.load_cases(CASES)[0]
    failed = cal.synthesize_failure(passing, "I will finish this next.", case_id="X01")
    assert failed.expected == "fail"
    assert failed.evidence[0] == "Final message from the agent: I will finish this next."
    assert failed.task == passing.task


def test_report_states_the_baseline() -> None:
    cases = cal.load_cases(CASES)
    text = cal.format_report(cal.calibrate(cases, cal.stub_judge("always-pass")))
    assert "always-pass=0.500" in text
    assert "does NOT beat" in text
