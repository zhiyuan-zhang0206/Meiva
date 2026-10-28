# meiva:note
# `meizel/budgets/budget_plans.py` unit tests: the two ceilings at their boundary and the order the
# report comes out in, which directory entries are candidates of the file budget, what counts as a
# Python file, which of a target and a structure directory is judged, and the judged directories with
# their entry counts. Asserted by value; the same ceilings against a real Git repository are covered
# by `test_limits.py` and `scripts/lint/tests/test_directory_budget_entries.py`.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

from pathlib import Path

import pytest

from meizel.budgets import budget_plans

_ROOT = Path("/repo")
_PACKAGE = _ROOT / "app"


def _text(lines: int) -> str:
    return "x = 1\n" * lines


@pytest.mark.parametrize(("lines", "flagged"), [(800, False), (801, True)])
def test_a_file_is_flagged_only_over_the_hard_ceiling(lines: int, flagged: bool) -> None:
    report = budget_plans.ceiling_violations({_PACKAGE / "big.py": _text(lines)}, _ROOT, {})
    assert bool(report) is flagged


def test_a_file_is_counted_in_the_lines_it_has_not_the_newline_it_ends_with() -> None:
    assert budget_plans.ceiling_violations({_PACKAGE / "a.py": _text(800)}, _ROOT, {}) == []
    assert budget_plans.ceiling_violations({_PACKAGE / "a.py": _text(801)}, _ROOT, {}) != []


def test_a_file_violation_names_the_path_the_count_and_the_guidance() -> None:
    [error] = budget_plans.ceiling_violations({_PACKAGE / "big.py": _text(801)}, _ROOT, {})
    assert error.startswith("app/big.py:801: file is 801 lines, over the 800-line hard ceiling — ")
    assert "cohesive subpackage" in error


@pytest.mark.parametrize(("entries", "flagged"), [(20, False), (21, True)])
def test_a_directory_is_flagged_only_over_the_entry_cap(entries: int, flagged: bool) -> None:
    report = budget_plans.ceiling_violations({}, _ROOT, {"app/pkg": entries})
    assert bool(report) is flagged


def test_a_directory_violation_names_the_path_the_count_and_the_guidance() -> None:
    [error] = budget_plans.ceiling_violations({}, _ROOT, {"app/pkg": 21})
    assert error.startswith("app/pkg: directory has 21 direct entries, over the 20-entry cap — ")
    assert "directory depth is unrestricted" in error


def test_the_report_orders_files_by_path_parts_then_directories_by_name() -> None:
    texts = {
        _PACKAGE / "c.py": _text(801),
        _PACKAGE / "a.py": _text(801),
        _PACKAGE / "a" / "b.py": _text(801),
    }
    report = budget_plans.ceiling_violations(texts, _ROOT, {"app/z": 21, "app/a": 21})
    assert [line.split(":")[0] for line in report] == [
        "app/a/b.py",
        "app/a.py",
        "app/c.py",
        "app/a",
        "app/z",
    ]


def test_a_report_with_nothing_over_a_ceiling_is_empty() -> None:
    assert budget_plans.ceiling_violations({}, _ROOT, {}) == []


@pytest.mark.parametrize(
    ("name", "is_symlink", "is_directory", "candidate"),
    [
        ("ok.py", False, False, True),
        ("sub", False, True, True),
        ("linked", True, False, False),
        (".hidden", False, True, False),
        ("__pycache__", False, True, False),
        ("migrations", False, True, False),
        ("migrations", False, False, True),
    ],
)
def test_a_candidate_entry_is_not_a_link_a_hidden_name_or_a_skipped_tree(
    name: str, is_symlink: bool, is_directory: bool, candidate: bool
) -> None:
    assert (
        budget_plans.entry_candidate(name, is_symlink=is_symlink, is_directory=is_directory)
        is candidate
    )


@pytest.mark.parametrize(
    ("name", "is_python"),
    [
        ("mod.py", True),
        ("mod.txt", False),
        ("mod.py.txt", False),
        ("py", False),
        ("mod.PY", False),
        ("migrations.py", True),
    ],
)
def test_only_a_python_suffix_is_counted(name: str, is_python: bool) -> None:
    assert budget_plans.is_python_file(name) is is_python


def test_a_target_under_the_scope_is_judged_as_itself() -> None:
    target = _ROOT / "meizel/budgets"
    assert budget_plans.judged_path(target, _ROOT / "meizel", _ROOT) == target


def test_a_scope_under_the_target_is_judged_as_the_scope() -> None:
    scope = _ROOT / "meizel/budgets"
    assert budget_plans.judged_path(_ROOT / "meizel", scope, _ROOT) == scope


def test_the_same_path_on_both_sides_is_judged_as_the_target() -> None:
    same = _ROOT / "meizel"
    assert budget_plans.judged_path(same, same, _ROOT) == same


def test_a_target_that_holds_the_repository_is_judged_as_the_structure_directory() -> None:
    root = Path("/work/repo")
    scope = root / "app"
    assert budget_plans.judged_path(Path("/work"), scope, root) == scope


def test_two_disjoint_paths_are_not_judged() -> None:
    assert budget_plans.judged_path(_ROOT / "base", _ROOT / "meizel", _ROOT) is None


@pytest.mark.parametrize("name", [".hidden", "__pycache__", "migrations"])
def test_a_judged_path_through_a_skipped_name_is_never_judged(name: str) -> None:
    assert budget_plans.judged_path(_ROOT / name, _ROOT, _ROOT) is None
    assert budget_plans.judged_path(_ROOT / "base" / name, _ROOT / "base", _ROOT) is None


def test_a_path_outside_the_repository_is_never_judged() -> None:
    assert budget_plans.judged_path(Path("/elsewhere"), _ROOT / "meizel", _ROOT) is None


def test_a_target_selects_its_descendants_and_its_ancestors_with_their_counts() -> None:
    children = {
        "": {"base", "docs"},
        "base": {"pkg"},
        "base/pkg": {"a.py", "b.py"},
        "docs": {"x.py"},
    }
    assert budget_plans.judged_directories(children, [_ROOT / "base/pkg"], _ROOT) == {
        "base": 1,
        "base/pkg": 2,
    }


def test_the_repository_root_selects_every_directory_but_not_itself() -> None:
    children = {"": {"base"}, "base": {"a.py"}}
    assert budget_plans.judged_directories(children, [_ROOT], _ROOT) == {"base": 1}


def test_a_directory_target_outside_the_repository_selects_nothing() -> None:
    children = {"": {"base"}, "base": {"a.py"}}
    assert budget_plans.judged_directories(children, [Path("/elsewhere")], _ROOT) == {}


def test_tracked_children_rejects_a_listing_without_a_terminator() -> None:
    with pytest.raises(ValueError, match="NUL-terminated"):
        budget_plans.tracked_children("a/b.py")


def test_tracked_children_rejects_a_path_git_could_not_have_written() -> None:
    with pytest.raises(ValueError, match="invalid tracked Git path"):
        budget_plans.tracked_children("../a.py\0")


def test_tracked_children_lists_every_directory_level() -> None:
    children = budget_plans.tracked_children("a/b/c.py\0a/d.py\0")
    assert children == {"": {"a"}, "a": {"b", "d.py"}, "a/b": {"c.py"}}


def test_tracked_children_does_not_count_a_manifest_as_an_entry() -> None:
    assert budget_plans.tracked_children("a/meizel.toml\0a/b.py\0") == {
        "": {"a"},
        "a": {"b.py"},
    }
