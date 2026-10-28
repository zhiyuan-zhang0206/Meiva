# meiva:note
# `meizel/budgets/budget_plans.py` unit tests: the two ceilings at their boundary, which directory
# entries are candidates of the file budget, which of a target and a structure directory is judged,
# which directories a target selects, and the wording of both reports. Asserted by value; the same
# ceilings against a real Git repository are covered by `test_limits.py` and
# `scripts/lint/tests/test_directory_budget_entries.py`.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

from pathlib import Path

import pytest

from meizel.budgets import budget_plans

_ROOT = Path("/repo")


@pytest.mark.parametrize(("count", "flagged"), [(800, False), (801, True)])
def test_a_file_is_flagged_only_over_the_hard_ceiling(count: int, flagged: bool) -> None:
    assert bool(budget_plans.file_violations({"base/big.py": count})) is flagged


def test_a_file_violation_names_the_path_the_count_and_the_guidance() -> None:
    [error] = budget_plans.file_violations({"base/big.py": 801})
    assert error.startswith("base/big.py:801: file is 801 lines, over the 800-line hard ceiling — ")
    assert "cohesive subpackage" in error


@pytest.mark.parametrize(("count", "flagged"), [(20, False), (21, True)])
def test_a_directory_is_flagged_only_over_the_entry_cap(count: int, flagged: bool) -> None:
    assert bool(budget_plans.directory_violations({"base/pkg": count})) is flagged


def test_a_directory_violation_names_the_path_the_count_and_the_guidance() -> None:
    [error] = budget_plans.directory_violations({"base/pkg": 21})
    assert error.startswith("base/pkg: directory has 21 direct entries, over the 20-entry cap — ")
    assert "directory depth is unrestricted" in error


def test_both_reports_are_in_path_order_and_hold_only_what_is_over_its_ceiling() -> None:
    lines = {"b/tall.py": 900, "a/tall.py": 900, "c/short.py": 1}
    assert [line.split(":")[0] for line in budget_plans.file_violations(lines)] == [
        "a/tall.py",
        "b/tall.py",
    ]
    assert budget_plans.file_violations({}) == []
    assert budget_plans.directory_violations({}) == []


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


def test_a_target_under_the_scope_is_judged_as_itself() -> None:
    assert budget_plans.judged_side(_ROOT / "meizel/budgets", _ROOT / "meizel", _ROOT) == "target"


def test_a_scope_under_the_target_is_judged_as_the_scope() -> None:
    assert budget_plans.judged_side(_ROOT / "meizel", _ROOT / "meizel/budgets", _ROOT) == "scope"


def test_the_same_path_on_both_sides_is_judged_as_the_target() -> None:
    assert budget_plans.judged_side(_ROOT / "meizel", _ROOT / "meizel", _ROOT) == "target"


def test_two_disjoint_paths_are_not_judged() -> None:
    assert budget_plans.judged_side(_ROOT / "base", _ROOT / "meizel", _ROOT) is None


@pytest.mark.parametrize("name", [".hidden", "__pycache__", "migrations"])
def test_a_judged_path_through_a_skipped_name_is_never_judged(name: str) -> None:
    assert budget_plans.judged_side(_ROOT / name, _ROOT, _ROOT) is None
    assert budget_plans.judged_side(_ROOT / "base" / name, _ROOT / "base", _ROOT) is None


def test_a_path_outside_the_repository_is_never_judged() -> None:
    assert budget_plans.judged_side(Path("/elsewhere"), _ROOT / "meizel", _ROOT) is None


def test_a_target_selects_its_descendants_and_its_ancestors() -> None:
    children = {"": {"base", "docs"}, "base": {"pkg"}, "base/pkg": {"a.py"}, "docs": {"x.py"}}
    assert budget_plans.selected_directories(children, [_ROOT / "base/pkg"], _ROOT) == {
        "base",
        "base/pkg",
    }


def test_the_repository_root_selects_every_directory_but_not_itself() -> None:
    children = {"": {"base"}, "base": {"a.py"}}
    assert budget_plans.selected_directories(children, [_ROOT], _ROOT) == {"base"}


def test_a_directory_target_outside_the_repository_selects_nothing() -> None:
    children = {"": {"base"}, "base": {"a.py"}}
    assert budget_plans.selected_directories(children, [Path("/elsewhere")], _ROOT) == set()


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
