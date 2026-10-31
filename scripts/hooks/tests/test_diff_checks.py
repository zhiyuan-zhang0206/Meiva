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


# meiva:note
# A layer no trigger ran prints `none` instead of disappearing: a missing check has to be visible
# in the line a reader trusts, not absent from it.
def test_the_receipt_orders_its_fields_and_names_what_nothing_filled() -> None:

    receipt = build_receipt({"pyright": "0err(1m36s)"}, "abc123", "def456")

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
    assert "pyright=0err(1m36s)" in receipt
    assert "selector=none" in receipt


# meiva:note
# The selector's own admission of blindness is the one field that may not be summarised: a BLIND
# change needs its fallback named beside it.
def test_a_selection_with_blind_files_names_them_and_keeps_the_decision_visible() -> None:

    assert Selection("SELECTED", ("a.py", "b.py"), ()).field == "SELECTED:2"
    assert Selection("SELECTED", ("a.py",), ("dyn.py",)).field == "BLIND(dyn.py)"


def _always(selection: Selection) -> Callable[[ChangeSet], Selection]:
    def choose(_change: ChangeSet) -> Selection:
        return selection

    return choose


# meiva:note
# Red is the exit code, not the text: a lint that fails while printing a reassuring word still
# reddens the receipt. The type check is the exception — its verdict is attribution, because a red
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


def test_a_green_run_reports_the_counts_its_tests_produced() -> None:
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
    assert values["selector"] == "SELECTED:1"
    assert values["passed"] == "645(6m40s)"
    assert "skipped" not in values


def test_a_change_that_touches_no_python_schedules_no_python_layer() -> None:
    lines = plan(_change("docs/readme.md"), skip_tests=True)

    assert lines[0] == "change: 1 files against origin/main"
    assert not any("pyright" in line for line in lines), (
        "a documentation-only change must not carry a type-check row the runner never runs"
    )


def test_the_trigger_names_its_judge_so_a_reader_can_see_what_red_means() -> None:
    assert Run("ruff", ("ruff", "check"), "exit code").judge == "exit code"


# meiva:note
# The table has more than one column of numbers; a receipt that read the wrong one would report a
# repayment as growth.
def test_the_debt_field_reads_the_tree_column_and_not_the_rules_one() -> None:

    table = (
        "code                   origin/main  new rules    tree   rules  content\n"
        "ambient-read                   102        102     104      +0       +2\n"
        "total                          264        264     279      +0      +15\n"
    )

    def execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
        if "debt" in str(argv):
            return 0, table, 3.0
        return 0, "0 errors, 0 warnings", 1.0

    values, _ = check(
        _change("scripts/thing.py"),
        skip_tests=True,
        execute=execute,
        choose=_always(Selection("SELECTED", (), ())),
    )

    assert values["debt"] == "ambient-read102->104,total264->279"


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
