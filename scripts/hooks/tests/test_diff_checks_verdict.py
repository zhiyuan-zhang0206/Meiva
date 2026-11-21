# meiva:note
# What the change-scoped check runner promises about the one verdict its receipt states.


from __future__ import annotations

import pytest

from scripts.hooks.diff_checks import build_receipt, receipt_verdict

# meiva:note
# The one conclusion a reader of the receipt trusts. A receipt whose fields are all clean states
# `pass`; each way a field can be red states `fail`, each way a layer can be unreadable or missing
# states `incomplete`, and `fail` outranks `incomplete`. `skipped(...)` and `n/a(...)` are not a
# verdict: they say a layer was not owed or runs elsewhere.
_CLEAN = {
    "scope": "python",
    "pyright": "0err(0s)",
    "err_in_this_diff": "0",
    "closure": "n/a(the-tree-was-green)",
    "ruff": "ok(0s)",
    "selector": "SELECTED:1files",
    "tests": "RAN(1files)",
    "passed": "20tests(1s)",
    "failed": "0tests",
    "errors": "0tests",
    "literal-tests": "n/a(covered-by-tests)",
    "blind": "n/a(no-changed-file-was-invisible)",
    "fallback": "n/a(the-selection-was-not-blind)",
    "vitest-app": "n/a(no-web-tree-file)",
    "next-build": "n/a(not-the-app-router)",
    "meizel": "ok(exit=0,779manifests,1edges)(0s)",
    "debt": "n/a(no-manifest-changed)",
    "docs-refs": "n/a(no-doc-touched)",
}
_BLIND = {
    "selector": "BLIND(dyn.py)",
    "blind": "BLIND(dyn.py)",
    "fallback": "n/a(a-blind-selection-has-no-fallback-yet)",
}


@pytest.mark.parametrize(
    ("overrides", "failed", "expected"),
    [
        ({}, False, "pass"),
        (
            {
                "tests": "skipped(--skip-tests)",
                "passed": "n/a(no-test-ran)",
                "failed": "n/a(no-test-ran)",
                "errors": "n/a(no-test-ran)",
            },
            False,
            "pass",
        ),
        ({"pyright": "9err(0in-closure,9elsewhere:other.py)(1s)"}, False, "pass"),
        (
            {"selector": "BLIND(dyn.py)", "blind": "BLIND(dyn.py)", "fallback": "RAN(domain)"},
            False,
            "pass",
        ),
        ({}, True, "fail"),
        ({"ruff": "failed(1)(0s)"}, False, "fail"),
        ({"meizel": "failed(exit=1,779manifests,1edges)(0s)"}, False, "fail"),
        ({"failed": "2tests"}, False, "fail"),
        ({"errors": "1tests"}, False, "fail"),
        ({"err_in_this_diff": "1"}, False, "fail"),
        ({"pyright": "1err(1in-closure,0elsewhere:none)(1s)"}, False, "fail"),
        ({"debt": "unreadable(no-table)(1s)"}, False, "incomplete"),
        (
            {"tests": "broken(no-report)", "passed": "n/a(broken)", "failed": "n/a(broken)"},
            False,
            "incomplete",
        ),
        ({"closure": "unknown"}, False, "incomplete"),
        (_BLIND, False, "incomplete"),
        ({"blind": "BLIND(dyn.py)"}, False, "incomplete"),
        (
            {
                "selector": "BLIND(dyn.py)",
                "blind": "BLIND(dyn.py)",
                "fallback": "RAN(domain)",
                "failed": "n/a(no-test-ran)",
                "errors": "n/a(no-test-ran)",
            },
            False,
            "incomplete",
        ),
        ({"debt": "unreadable(no-table)(1s)", "failed": "2tests"}, False, "fail"),
        ({"literal-tests": "0hits"}, False, "pass"),
        ({"literal-tests": "ok(2files,15passed)(2s)"}, False, "pass"),
        ({"literal-tests": "over-cap(14files>10)"}, False, "pass"),
        ({"literal-tests": "skipped(--skip-literal-tests)"}, False, "pass"),
        ({"literal-tests": "failed(2files,1failed,0errors)(2s)"}, False, "fail"),
        ({"literal-tests": "broken(no-report)"}, False, "incomplete"),
    ],
)
def test_the_receipt_states_one_verdict_for_every_way_a_field_can_read(
    overrides: dict[str, str], failed: bool, expected: str
) -> None:
    values = {**_CLEAN, **overrides}
    assert receipt_verdict(values, failed=failed) == expected
    assert f" verdict={expected} " in build_receipt(values, "abc123", "def456", failed=failed)


def test_a_field_the_run_never_filled_makes_the_receipt_incomplete() -> None:
    values = {label: value for label, value in _CLEAN.items() if label != "debt"}
    assert receipt_verdict(values, failed=False) == "incomplete"
