# meiva:note
# Where the project-local skill search looks, asserted as paths: the markers of a git root, the
# directories tried going up and the skill folders a repository may carry in precedence order.
# `test_project_roots.py` probes real repositories with them.

from __future__ import annotations

from pathlib import Path

from base.packages.skills.project_root_plan import (
    ancestor_chain,
    git_marker_paths,
    skill_folder_candidates,
)


def test_a_git_root_is_marked_by_a_dot_git_file_or_a_dot_git_head() -> None:
    assert git_marker_paths(Path("/work/repo")) == (
        Path("/work/repo/.git"),
        Path("/work/repo/.git/HEAD"),
    )


def test_the_search_goes_from_the_start_up_to_the_filesystem_root() -> None:
    assert ancestor_chain(Path("/a/b/c")) == (
        Path("/a/b/c"),
        Path("/a/b"),
        Path("/a"),
        Path("/"),
    )


def test_the_filesystem_root_is_its_own_chain() -> None:
    assert ancestor_chain(Path("/")) == (Path("/"),)


def test_skill_folders_are_listed_in_the_order_the_last_one_wins() -> None:
    assert skill_folder_candidates(Path("/work/repo")) == (
        Path("/work/repo/.claude/skills"),
        Path("/work/repo/.agents/skills"),
        Path("/work/repo/.meiva/skills"),
    )
