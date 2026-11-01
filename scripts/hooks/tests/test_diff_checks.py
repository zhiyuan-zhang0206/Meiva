# meiva:note
# What the change-scoped check runner promises about its plan and its receipt.


from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from scripts.hooks.diff_checks import (
    FIELD_ORDER,
    ChangeSet,
    Run,
    Selection,
    build_receipt,
    check,
    child_environment,
    plan,
    pytest_counts,
    run_child,
)


# meiva:note
# A fake run still has to leave the artefact the reader now reads: the report file is what these
# tests stand in for, so the fake writes one instead of printing a summary line.
def _write_report(argv: Sequence[str], *, passed: int, failed: int = 0, errors: int = 0) -> None:
    named = next(part for part in argv if part.startswith("--junitxml="))
    cases = "".join(f'<testcase classname="t" name="p{i}" />' for i in range(passed))
    cases += "".join(
        f'<testcase classname="t" name="f{i}"><failure /></testcase>' for i in range(failed)
    )
    cases += "".join(
        f'<testcase classname="t" name="e{i}"><error /></testcase>' for i in range(errors)
    )
    Path(named.split("=", 1)[1]).write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" tests="{passed + failed + errors}">'
        f"{cases}</testsuite></testsuites>"
    )


def _change(*files: str) -> ChangeSet:
    return ChangeSet(root=Path("/repo"), base="origin/main", files=files)


def _always(selection: Selection) -> Callable[[ChangeSet], Selection]:
    def choose(_change: ChangeSet) -> Selection:
        return selection

    return choose


def _run(values: dict[str, str], files: tuple[str, ...]) -> tuple[dict[str, str], bool]:
    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        return 0, "0 errors, 0 warnings", 1.0

    return check(
        _change(*files),
        skip_tests=True,
        execute=execute,
        choose=_always(Selection("SELECTED", (), ())),
    )


# meiva:note
# A field is never absent and never `none`: a check this change cannot need says `n/a(<why>)`, so
# "did not apply" is a value a reader sees rather than an absence they have to interpret.
def test_the_receipt_states_every_field_and_never_prints_a_bare_none() -> None:

    values, _ = _run({}, ("docs/readme.md",))
    receipt = build_receipt(values, "abc123", "def456")

    assert receipt.startswith("diff-checks/v1 @abc123 base=def456 "), (
        "a receipt without its base cannot be replayed: `selector=` and `debt=` are both "
        "measurements against a baseline the reader would have to guess"
    )
    assert receipt.endswith(" generated=scripts/hooks/diff_checks.py")
    fields = [part.split("=", 1)[0] for part in receipt.split()[3:-1]]
    assert fields == list(FIELD_ORDER), (
        "the field order is the reading order; a new field appended anywhere else moves what a "
        "reviewer sees first"
    )
    assert " none " not in receipt and not receipt.endswith("=none"), (
        "`none` cannot tell a layer that did not apply from one that ran and found nothing"
    )
    assert "pyright=n/a(no-python-touched)" in receipt
    assert "scope=docs" in receipt


# meiva:note
# A documentation-only change owes the document checks, not a type check it cannot fail; and the
# layers it does not owe say so instead of vanishing.
def test_a_documentation_change_runs_the_document_checks_and_says_why_the_rest_are_absent() -> None:
    lines = plan(_change("docs/readme.md"), skip_tests=True)
    joined = "\n".join(lines)

    assert any("docs-refs" in line for line in lines)
    assert any("docs-okf" in line for line in lines)
    assert any("meizel" in line for line in lines), (
        "the checker reads manifests and role lists for every change, documentation included"
    )
    assert "pyright" not in joined, (
        "a documentation-only change must not carry a type check the change cannot fail"
    )
    values, _ = _run({}, ("docs/readme.md",))
    assert values["pyright"] == "n/a(no-python-touched)"


# meiva:note
# The comparison against the base costs minutes and can only move when a manifest moves, so a
# Python change that touches none does not pay for it.
def test_a_python_change_without_a_manifest_skips_the_debt_comparison() -> None:
    values, _ = _run({}, ("base/thing.py",))

    assert values["debt"] == "n/a(no-manifest-changed)"
    with_manifest = plan(_change("base/thing.py", "base/meizel.toml"), skip_tests=True)
    assert any("debt" in line for line in with_manifest)


# meiva:note
# The selector's own admission of blindness is the one field that may not be summarised: a BLIND
# change needs its fallback named beside it.
def test_a_selection_with_blind_files_names_them_and_keeps_the_decision_visible() -> None:

    assert Selection("SELECTED", ("a.py", "b.py"), ()).field == "SELECTED:2files"
    assert Selection("SELECTED", ("a.py",), ("dyn.py",)).field == "BLIND(dyn.py)"


# meiva:note
# Red is the exit code, not the text: a lint that fails while printing a reassuring word still
# reddens the receipt. The type check is the exception -- its verdict is attribution, because a red
# tree is not the same claim as a red tree this change can reach.
def test_a_blocking_run_that_exits_non_zero_turns_the_receipt_red() -> None:

    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        if "ruff" in str(argv[0]):
            return 1, "all good, promise", 1.0
        return 0, "0 errors, 0 warnings", 1.0

    values, failed = check(
        _change("base/thing.py"),
        skip_tests=True,
        execute=execute,
        choose=_always(Selection("SELECTED", (), ())),
    )

    assert failed is True
    assert values["ruff"].startswith("failed(1)")


# meiva:note
# Two counts of different things share the line: the selector chooses files and pytest counts
# tests, and a receipt that prints both without their unit invites the reader to compare them.
def test_a_green_run_reports_the_counts_its_tests_produced_with_their_units() -> None:
    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        if "pytest" in str(argv[:3]):
            _write_report(argv, passed=645)
            return 0, "", 400.0
        return 0, "0 errors, 0 warnings", 96.0

    values, failed = check(
        _change("base/thing.py"),
        skip_tests=False,
        execute=execute,
        choose=_always(Selection("SELECTED", ("tests/test_a.py",), ())),
    )

    assert failed is False
    assert values["selector"] == "SELECTED:1files"
    assert values["tests"] == "RAN(1files)"
    assert values["passed"] == "645tests(6m40s)"
    assert values["failed"] == "0tests"


# meiva:note
# A run that did not execute the tests reports no count at all, in the runner as well as in the
# checker: the same field cannot mean "not run" and "0 passed".
def test_a_skipped_test_run_says_so_instead_of_reporting_counts() -> None:
    values, _ = _run({}, ("base/thing.py",))

    assert values["tests"] == "skipped(--skip-tests)"
    assert values["passed"] == "n/a(no-test-ran)"
    assert values["failed"] == "n/a(no-test-ran)"


def test_the_trigger_names_its_judge_so_a_reader_can_see_what_red_means() -> None:
    assert Run("ruff", ("ruff", "check"), "exit code").judge == "exit code"


# meiva:note
# The table has more than one column of numbers and the reference column is named after whatever
# `--against` was given: a receipt pinned to a sha (a replay, or a pull request base) must read the
# same numbers as one pinned to `origin/main`.
def test_the_debt_field_reads_its_columns_by_position_not_by_the_reference_name() -> None:
    rule_table = (
        "code                    2bc728bea  new rules    tree   rules  content\n"
        "ambient-read                   102        102     104      +0       +2\n"
        "total                          264        264     279      +0      +15\n"
    )
    plain_table = (
        "code                   origin/main    tree  change\n"
        "ambient-read                   102     104      +2\n"
        "total                          264     279     +15\n"
    )

    def run(table: str) -> tuple[dict[str, str], bool]:
        def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
            return (0, table, 3.0) if "debt" in str(argv) else (0, "0 errors, 0 warnings", 1.0)

        return check(
            _change("scripts/thing.py", "scripts/meizel.toml"),
            skip_tests=True,
            execute=execute,
            choose=_always(Selection("SELECTED", (), ())),
        )

    rules, _ = run(rule_table)
    plain, _ = run(plain_table)

    assert rules["debt"] == "ok(ambient-read102->104,total264->279)(3s)"
    assert plain["debt"] == "ok(ambient-read102->104,total264->279)(3s)"


# meiva:note
# Output the runner cannot read is not a pass: it says so in the field, and the checker refuses it
# rather than printing a green layer nobody measured.
def test_debt_output_without_a_table_is_reported_as_unreadable() -> None:
    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        return (0, "meizel: okay", 3.0) if "debt" in str(argv) else (0, "0 errors", 1.0)

    values, _ = check(
        _change("scripts/meizel.toml"),
        skip_tests=True,
        execute=execute,
        choose=_always(Selection("SELECTED", (), ())),
    )

    assert values["debt"].startswith("unreadable")


# meiva:note
# The React Server Component boundary refuses a client hook in a server component at build time
# only: type checking, linting and unit tests all pass, which is how a broken page shipped today.
def test_a_change_under_the_app_router_schedules_the_production_build() -> None:
    lines = plan(_change("ui/web/src/app/fleet/page.tsx"), skip_tests=True)

    assert any("next-build" in line for line in lines), (
        "a file under ui/web/src/app/ moves code across the RSC boundary; only the production "
        "build refuses it"
    )
    assert any("ui/web" in line for line in lines), "the build runs in the frontend package"


def test_a_change_that_cannot_cross_the_boundary_skips_the_build() -> None:
    lines = plan(_change("ui/web/src/lib/browser/browser.ts", "base/thing.py"), skip_tests=True)

    assert not any("next-build" in line for line in lines)


# meiva:note
# A red tree is attributed, not assumed: errors in files that cannot see the change are named so a
# reader can check them, and the change stays green when none of them are its own.
def test_errors_outside_the_closure_are_named_and_do_not_fail_the_change(tmp_path: Path) -> None:
    (tmp_path / "changed.py").write_text("")
    (tmp_path / "caller.py").write_text("from changed import thing\n")
    (tmp_path / "unrelated.py").write_text("import json\n")
    output = f"{tmp_path}/unrelated.py:3:1 - error: something else\n1 error, 0 warnings, 0 notes"

    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        return (1, output, 2.0) if "basedpyright" in str(argv[0]) else (0, "ok", 1.0)

    values, failed = check(
        ChangeSet(root=tmp_path, base="origin/main", files=("changed.py",)),
        skip_tests=True,
        execute=execute,
        choose=_always(Selection("SELECTED", (), ())),
    )

    assert failed is False, "an error the change cannot reach is not this change's"
    assert "0in-closure" in values["pyright"]
    assert "unrelated.py" in values["pyright"]


def test_an_error_in_a_file_that_imports_the_change_fails_it(tmp_path: Path) -> None:
    (tmp_path / "changed.py").write_text("")
    (tmp_path / "caller.py").write_text("from changed import thing\n")
    output = f"{tmp_path}/caller.py:3:1 - error: missing argument\n1 error, 0 warnings, 0 notes"

    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        return (1, output, 2.0) if "basedpyright" in str(argv[0]) else (0, "ok", 1.0)

    values, failed = check(
        ChangeSet(root=tmp_path, base="origin/main", files=("changed.py",)),
        skip_tests=True,
        execute=execute,
        choose=_always(Selection("SELECTED", (), ())),
    )

    assert failed is True, (
        "a signature change breaks its callers, and those files are the ones it never touched"
    )
    assert "1in-closure" in values["pyright"]


# meiva:note
# A bare `ok` cannot tell "read the whole tree" from "found nothing to read": the checker's claim
# belongs in the receipt beside its verdict.
def test_the_meizel_field_carries_what_the_checker_covered() -> None:
    summary = "meizel: ok (76697 fact edges, 772 manifests; 217 recorded debts remain)"

    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        if "meizel" in str(argv):
            return 0, summary, 17.0
        return 0, "0 errors, 0 warnings", 1.0

    values, _ = check(
        _change("scripts/thing.py"),
        skip_tests=True,
        execute=execute,
        choose=_always(Selection("SELECTED", (), ())),
    )

    assert values["meizel"] == "ok(exit=0,772manifests,76697edges)(17s)"


# meiva:note
# A green exit code is not the same fact as "the tests ran": the selector never chooses the app
# tree whole, so a page test can sit red for days while every run it received was green.
def test_a_change_anywhere_in_the_web_tree_runs_the_whole_app_test_tree() -> None:
    lines = plan(_change("ui/web/src/lib/browser/browser.ts"), skip_tests=True)

    assert any("vitest-app" in line for line in lines), (
        "a change under ui/web/ can break a page no selector names; only the whole app tree "
        "answers for it"
    )
    assert any("src/app" in line for line in lines), (
        "the plan has to name which tests it means, not only that the package runs something"
    )
    assert any("(in ui/web)" in line for line in lines)


def test_a_change_outside_the_web_tree_runs_no_app_tests() -> None:
    lines = plan(_change("base/thing.py"), skip_tests=True)

    assert not any("vitest-app" in line for line in lines)


def test_the_app_tree_field_reports_the_denominator_and_not_only_the_verdict() -> None:
    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        if "vitest" in str(argv):
            return 0, " Test Files  34 passed (34)\n      Tests  331 passed (331)\n", 11.0
        return 0, "0 errors, 0 warnings", 96.0

    values, failed = check(
        _change("ui/web/src/lib/browser/browser.ts"),
        skip_tests=True,
        execute=execute,
        choose=_always(Selection("SELECTED", (), ())),
    )

    assert failed is False
    assert values["vitest-app"] == "ok(331tests)(11s)", (
        "a run that answered no test must not read like one that answered 331"
    )


def test_the_app_tree_still_fails_the_receipt_when_its_tests_do() -> None:
    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        if "vitest" in str(argv):
            return 1, "      Tests  2 failed | 329 passed (331)\n", 11.0
        return 0, "0 errors, 0 warnings", 96.0

    values, failed = check(
        _change("ui/web/src/lib/browser/browser.ts"),
        skip_tests=True,
        execute=execute,
        choose=_always(Selection("SELECTED", (), ())),
    )

    assert failed is True
    assert values["vitest-app"] == "failed(1)(331tests)(11s)"


# meiva:note
# Vitest colours its summary, and the escape sequences land between the indent and the word
# `Tests`: a parser that only matched uncoloured output reported zero tests for a green run. The
# 2026-10-31 check against the real tree answered `Test Files 34 passed (34)` while this parser
# said 0.
def test_the_app_tree_parser_reads_a_coloured_summary() -> None:
    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        if "vitest" in str(argv):
            return (
                0,
                (
                    "\x1b[2m Test Files \x1b[22m \x1b[1m\x1b[32m34 passed\x1b[39m\x1b[22m\x1b[90m (34)\x1b[39m\n"
                    "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m331 passed\x1b[39m\x1b[22m\x1b[90m (331)\x1b[39m\n"
                ),
                6.0,
            )
        return 0, "0 errors, 0 warnings", 96.0

    values, failed = check(
        _change("ui/web/src/lib/browser/browser.ts"),
        skip_tests=True,
        execute=execute,
        choose=_always(Selection("SELECTED", (), ())),
    )

    assert failed is False
    assert values["vitest-app"] == "ok(331tests)(6s)"


# meiva:note
# A judged child runs in the contract CI gives it, not in the operator's shell: a `MEIVA_*` the
# launcher exported must not reach it (the suite drops those keys mid-session and then reds about
# the environment it started from — #5907), while everything that locates this box's tools must,
# because CI's paths do not exist here. The child here is `env`, so the assertion reads the
# environment the child actually received, not one this process happens to share with it.
def test_a_judged_child_loses_the_callers_product_namespace_and_keeps_its_toolchain(
    tmp_path: Path,
) -> None:
    caller = {"PATH": "/usr/bin:/bin", "TOOLCHAIN_LEAK_PROBE": "kept", "MEIVA_LEAK_PROBE": "1"}

    code, output, _ = run_child(("env",), tmp_path, environment=child_environment(caller))

    assert code == 0
    assert set(output.splitlines()) == {"PATH=/usr/bin:/bin", "TOOLCHAIN_LEAK_PROBE=kept"}
    assert child_environment({"MEIVA_HOME": "/operator/home", "HOME": "/operator/home"}) == {
        "HOME": "/operator/home"
    }, "one rule cuts the product namespace; an allowlist of toolchain names would be a second"


# meiva:note
# pytest's summary counts a test that could not start as an error and leaves `failed` at 0, so a
# receipt that parsed only the failure count reported a run of 31151 passed and 538 errors as
# `failed=0tests` (#8001, 2026-11-01). The count is its own field, and a skipped run says so
# instead of printing a zero nobody measured.
def test_errors_are_counted_beside_failures_and_never_folded_into_them() -> None:
    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        if "-m" in argv and "pytest" in argv:
            _write_report(argv, passed=10, errors=3)
            return 1, "", 1.2
        return 0, "0 errors, 0 warnings", 1.0

    values, failed = check(
        _change("base/thing.py"),
        skip_tests=False,
        execute=execute,
        choose=_always(Selection("SELECTED", ("tests/test_a.py",), ())),
    )

    assert failed is True, "the shell saw exit 1; the receipt must not read as green"
    assert values["passed"] == "10tests(1s)"
    assert values["failed"] == "0tests"
    assert values["errors"] == "3tests", "the three tests that never ran are the whole story here"

    skipped, _ = _run({}, ("base/thing.py",))
    assert skipped["errors"] == "n/a(no-test-ran)"


# meiva:note
# A truncated run is not a red one. These pin the three states the `tests` field may carry and the
# reason each is named: a report that never arrived, a report with fewer results than files were
# selected, and a whole report whose failures mean what they say.
def _case(name: str, *, outcome: str = "passed") -> str:
    inner = "" if outcome == "passed" else f"<{outcome}/>"
    return f'<testcase classname="t" name="{name}">{inner}</testcase>'


def _report(path: Path, cases: list[str]) -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" tests="{len(cases)}">{"".join(cases)}</testsuite>'
        "</testsuites>"
    )
    return path


def test_a_run_without_a_report_is_broken_not_failed(tmp_path: Path) -> None:
    counts = pytest_counts(tmp_path / "absent.xml", selected_files=1990, seconds=1.0)

    assert counts.values["tests"] == "broken(no-report)"
    assert counts.values["failed"] == "n/a(broken)"
    assert counts.broken is True


def test_a_report_that_covers_fewer_files_than_were_selected_is_broken(tmp_path: Path) -> None:
    report = _report(tmp_path / "short.xml", [_case("a"), _case("b", outcome="failure")])

    counts = pytest_counts(report, selected_files=1990, seconds=1.0)

    assert counts.values["tests"] == "broken(short-run)", (
        "five results for 1990 files is a run that died, not eighteen failures: the receipt must "
        "say which one it is"
    )
    assert counts.values["passed"] == "n/a(broken)"
    assert counts.broken is True


def test_a_whole_report_counts_what_it_ran(tmp_path: Path) -> None:
    report = _report(
        tmp_path / "whole.xml",
        [_case("a"), _case("b"), _case("c", outcome="failure"), _case("d", outcome="skipped")],
    )

    counts = pytest_counts(report, selected_files=3, seconds=2.0)

    assert counts.values["tests"] == "RAN(3files)"
    assert counts.values["passed"] == "2tests(2s)"
    assert counts.values["failed"] == "1tests"
    assert counts.values["errors"] == "0tests"
    assert counts.broken is False


# meiva:note
# The floor is the file count, not the per-file average: a selected file that yields no result at
# all (fixtures only, a `collect_ignore`, a file-level skip) must not turn a whole run broken. The
# four files here produced 31 results between them, and the verdict does not move.
def test_a_file_that_yields_no_result_does_not_make_a_whole_run_broken(tmp_path: Path) -> None:
    report = _report(tmp_path / "mixed.xml", [_case(f"t{i}") for i in range(31)])

    counts = pytest_counts(report, selected_files=4, seconds=1.0)

    assert counts.values["tests"] == "RAN(4files)"
    assert counts.broken is False


# meiva:note
# The other way a run produces no evidence: `--junitxml` into a directory that is not there. pytest
# cannot write the report, nothing counts, and the receipt must say `broken(no-report)` rather than
# quietly reporting a run of zero results as green.
def test_a_report_that_cannot_be_written_reads_as_broken(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    # meiva:note
    # A regular file where the report's directory would go: pytest cannot create it, so the run
    # ends as a usage error with nothing written -- exactly the shape the reader must not mistake
    # for a run of zero tests.
    (tmp_path / "blocked").write_text("not a directory")
    report = tmp_path / "blocked" / "junit.xml"

    code, _output, seconds = run_child(
        (sys.executable, "-m", "pytest", "-q", f"--junitxml={report}", "test_ok.py"),
        tmp_path,
        environment=child_environment(os.environ),
    )
    counts = pytest_counts(report, selected_files=1, seconds=seconds)

    assert code != 0
    assert counts.values["tests"] == "broken(no-report)"
    assert counts.broken is True


# meiva:note
# The acceptance case: a session that dies mid-run, its workers with it.
#
# The old reader parsed pytest's summary line, so a run that never reached its end still produced
# a number -- the same number a real failure produces. The report file cannot lie that way: a
# session killed before it finishes leaves no report, and the receipt says `broken(no-report)`
# instead of counting what happened to finish.
def test_a_session_killed_mid_run_reads_as_broken_not_failed(tmp_path: Path) -> None:
    (tmp_path / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(60)\n"
    )
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    report = tmp_path / "junit.xml"

    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603
            (
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-n",
                "2",
                f"--junitxml={report}",
                "test_slow.py",
                "test_ok.py",
            ),
            cwd=tmp_path,
            timeout=8,
            check=False,
            env=child_environment(os.environ),
        )

    counts = pytest_counts(report, selected_files=2, seconds=8.0)

    assert counts.values["tests"] == "broken(no-report)", counts.values["tests"]
    assert counts.values["failed"] == "n/a(broken)", (
        "a killed session must never read as `failed=Ntests`: the reader cannot tell that from a "
        "real failure, and neither can the merge that trusts it"
    )
    assert counts.broken is True
