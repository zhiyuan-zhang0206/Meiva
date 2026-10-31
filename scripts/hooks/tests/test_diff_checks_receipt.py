# meiva:note
# The receipt checker's job is to refuse a line a reader would misread: no head, no base, a
# silent skip, a blind selection without its fallback, or unreplaced refusals nobody answered.
from __future__ import annotations

import pytest

from scripts.hooks.diff_checks_receipt import findings, parse, reasons_named

GOOD = (
    "diff-checks/v1 @abc123 base=def456 pyright=0err(1m36s) err_in_this_diff=0 ruff=ok(0s) "
    "selector=SELECTED:645 tests=RAN passed=645(6m40s) failed=0 fallback=none blind=none "
    "vitest=none meizel=ok(17s) debt=ambient-read102->104,total264->279 refusal-loss=none "
    "skipped=none generated=scripts/hooks/diff_checks.py"
)


def _line(drop: str = "", **overrides: str) -> str:
    receipt: dict[str, str] = {}
    for part in GOOD.split()[2:]:
        name, _, value = part.partition("=")
        receipt[name] = value
    receipt.update(overrides)
    receipt.pop(drop, None)
    return " ".join(("diff-checks/v1 @abc123", *(f"{k}={v}" for k, v in receipt.items())))


def test_a_complete_receipt_for_this_head_has_no_findings() -> None:
    assert findings(parse(GOOD), head="abc123", body="") == []


def test_a_receipt_from_another_head_is_refused() -> None:
    problems = findings(parse(GOOD), head="999999", body="")

    assert problems and "produced for abc123" in problems[0]


def test_a_missing_field_is_named_rather_than_treated_as_green() -> None:
    problems = findings(parse(_line(drop="base")), head="abc123", body="")

    assert any("missing fields" in problem for problem in problems)


def test_a_skipped_layer_must_carry_its_reason() -> None:
    assert reasons_named("none")
    assert reasons_named("tests(--skip-tests)")
    assert not reasons_named("tests")

    problems = findings(parse(_line(skipped="tests")), head="abc123", body="")

    assert any("reason" in problem for problem in problems)


def test_a_skipped_run_must_not_report_a_passed_count() -> None:
    problems = findings(
        parse(_line(tests="SKIPPED(--skip-tests)", passed="645(1s)")), head="abc123", body=""
    )

    assert any("skipped run" in problem for problem in problems)


def test_a_blind_selection_without_its_fallback_is_refused() -> None:
    problems = findings(parse(_line(blind="dyn.py")), head="abc123", body="")
    answered = findings(
        parse(_line(blind="dyn.py", fallback="RAN(scripts/ci)")), head="abc123", body=""
    )

    assert any("fallback" in problem for problem in problems)
    assert answered == []


def test_unreplaced_refusals_need_the_reviewer_line_in_the_body() -> None:
    receipt = parse(_line(**{"refusal-loss": "hits=12,blocking=2,unreplaced=3"}))

    assert findings(receipt, head="abc123", body="## What and why")
    assert (
        findings(receipt, head="abc123", body="refusal-loss: 3 unreplaced, reviewed: a, b, c") == []
    )


def test_a_line_that_is_not_a_receipt_is_refused() -> None:
    with pytest.raises(ValueError):
        parse("pyright=0err not a receipt")
