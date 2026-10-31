# meiva:note
# What the change-scoped check runner promises about its plan and its receipt.


from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from scripts.hooks.diff_checks import (
    FIELD_ORDER,
    ChangeSet,
    Run,
    Selection,
    build_receipt,
    check,
    plan,
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
            return 0, "645 passed in 400s", 400.0
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
