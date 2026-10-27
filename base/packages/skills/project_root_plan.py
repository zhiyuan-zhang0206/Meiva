# meiva:note
# Where the project-local skill search looks, as paths and no more: which paths mark a directory as
# a git repository root, the directories to try going up from a start, and the skill folders a
# repository root may carry in precedence order. `project_roots.py` probes the filesystem for them.

from __future__ import annotations

from pathlib import Path


# meiva:note
# The paths that mark `directory` as a git repository or worktree root: a `.git` file (linked
# worktree, submodule) or a `.git` directory with a `HEAD`. An empty or foreign `.git` has neither,
# so it is skipped, as git itself keeps looking upward past it.
def git_marker_paths(directory: Path) -> tuple[Path, Path]:
    dotgit = directory / ".git"
    return dotgit, dotgit / "HEAD"


# meiva:note
# The directories to try for a repository root from a resolved start: itself, then each parent up
# to the filesystem root.
def ancestor_chain(resolved: Path) -> tuple[Path, ...]:
    return (resolved, *resolved.parents)


# meiva:note
# The project-local skill folders a repository root may carry, in the order they are scanned with
# last-wins: `.claude/skills` (Claude Code compatibility), `.agents/skills` (the open Agent Skills
# standard directory) and `.meiva/skills` (Meiva's own repo-local skills), so a Meiva-native skill
# overrides a same-named compat one.
def skill_folder_candidates(root: Path) -> tuple[Path, Path, Path]:
    return (
        root / ".claude" / "skills",
        root / ".agents" / "skills",
        root / ".meiva" / "skills",
    )
