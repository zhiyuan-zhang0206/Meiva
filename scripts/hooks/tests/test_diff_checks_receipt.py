# meiva:note
# The receipt checker's job is to refuse a line a reader would misread: no head, no base, a field
# with neither a result nor a reason, a layer whose output could not be read, a passed count beside
# a skipped run, a blind selection without its fallback, or unreplaced refusals nobody answered.
from __future__ import annotations

import pytest

from scripts.hooks.diff_checks_receipt import absence_names_reason, findings, parse

GOOD = (
    "diff-checks/v1 @abc123 base=def456 scope=python pyright=0err(1m50s) err_in_this_diff=0 "
    "closure=n/a(the-tree-was-green) ruff=ok(1s) selector=SELECTED:2files tests=RAN(2files) "
    "passed=645tests(6m40s) failed=0tests blind=n/a(no-changed-file-was-invisible) "
    "fallback=n/a(the-selection-was-not-blind) vitest=n/a(no-app-tree-file) "
    "next-build=n/a(not-the-app-router) meizel=ok(exit=0,772manifests,76750edges)(22s) "
    "debt=n/a(no-manifest-changed) refusal-loss=n/a(no-net-debt-reduction) "
    "docs-refs=n/a(no-doc-touched) docs-okf=n/a(no-doc-touched) "
    "generated=scripts/hooks/diff_checks.py"
)

SKIPPED = (
    GOOD.replace("tests=RAN(2files)", "tests=skipped(--skip-tests)")
    .replace("passed=645tests(6m40s)", "passed=n/a(no-test-ran)")
    .replace("failed=0tests", "failed=n/a(no-test-ran)")
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


def test_a_run_that_skipped_its_tests_still_passes_the_checker() -> None:
    assert findings(parse(SKIPPED), head="abc123", body="") == []


# meiva:note
# The defect this pins: `passed=none` is a truthy string, so a skipped run that reported it tripped
# the very rule meant to catch a real count -- the tool rejected three of the receipts it produced
# itself. A skipped run reports no count; if one is present, that is the finding.
def test_a_skipped_run_reporting_passed_none_is_refused() -> None:
    problems = findings(
        parse(_line(tests="skipped(--skip-tests)", passed="none", failed="none")),
        head="abc123",
        body="",
    )

    assert any("nor a reason" in problem for problem in problems), (
        "`none` is not a count and not a reason: the receipt says what happened in words"
    )
    assert any("skipped run" in problem for problem in problems)


def test_a_skipped_run_reporting_a_real_count_is_refused() -> None:
    problems = findings(
        parse(_line(tests="skipped(--skip-tests)", passed="645tests(1s)")), head="abc123", body=""
    )

    assert any("skipped run" in problem for problem in problems)


def test_a_run_that_executed_tests_must_report_both_counts_with_units() -> None:
    problems = findings(parse(_line(passed="645(6m40s)", failed="0")), head="abc123", body="")

    assert any("passed=<n>tests" in problem for problem in problems)


def test_a_receipt_from_another_head_is_refused() -> None:
    problems = findings(parse(GOOD), head="999999", body="")

    assert problems and "produced for abc123" in problems[0]


def test_a_missing_field_is_named_rather_than_treated_as_green() -> None:
    problems = findings(parse(_line(drop="base")), head="abc123", body="")

    assert any("missing fields" in problem for problem in problems)


# meiva:note
# A layer whose output the runner could not read never passes as green: the runner fixes what it
# can read, and the checker refuses the rest instead of trusting the absence of a verdict.
def test_a_layer_the_runner_could_not_read_is_refused() -> None:
    problems = findings(parse(_line(debt="ok(unreadable:no-table)(3s)")), head="abc123", body="")

    assert any("could not read" in problem for problem in problems)


def test_an_absence_names_its_reason() -> None:
    assert absence_names_reason("n/a(no-python-touched)")
    assert absence_names_reason("skipped(--skip-tests)")
    assert not absence_names_reason("n/a(")

    problems = findings(parse(_line(ruff="n/a(")), head="abc123", body="")

    assert any("reason" in problem for problem in problems)


def test_a_blind_selection_without_its_fallback_is_refused() -> None:
    problems = findings(parse(_line(blind="BLIND(dyn.py)")), head="abc123", body="")
    answered = findings(
        parse(_line(blind="BLIND(dyn.py)", fallback="RAN(scripts/ci)")), head="abc123", body=""
    )

    assert any("fallback" in problem for problem in problems)
    assert answered == []


def test_unreplaced_refusals_need_the_reviewer_line_in_the_body() -> None:
    receipt = parse(_line(**{"refusal-loss": "ok(hits=12,blocking=2,unreplaced=3)(1s)"}))

    assert findings(receipt, head="abc123", body="## What and why")
    assert (
        findings(receipt, head="abc123", body="refusal-loss: 3 unreplaced, reviewed: a, b, c") == []
    )


def test_a_line_that_is_not_a_receipt_is_refused() -> None:
    with pytest.raises(ValueError):
        parse("pyright=0err not a receipt")
