"""The 800-line file ceiling and the 20-entry directory cap."""

from __future__ import annotations

from pathlib import Path

from meizel.budgets import directory_budget
from meizel.graph.universe import GitRepo
from meizel.lint_common import read_utf8_text

__all__ = ["DIRECTORY_CEILING", "HARD_CEILING", "budget_files", "check"]

HARD_CEILING = 800
DIRECTORY_CEILING = 20
_FILE_CEILING_GUIDANCE = (
    "the file budget leaves room for cohesive logic behind a small interface; review "
    "responsibility boundaries, separate pure decisions from I/O orchestration, and move "
    "related implementation into a cohesive subpackage with a narrow meizel.toml exports "
    "door; avoid mechanical _part2 or _helpers splits"
)
_DIRECTORY_CEILING_GUIDANCE = (
    "the entry cap bounds the concepts at one directory, not total project size; directory "
    "depth is unrestricted, so group related behavior into cohesive subpackages with narrow "
    "meizel.toml exports doors"
)


def budget_files(
    targets: list[Path], repo_root: Path, structure_dirs: tuple[str, ...]
) -> set[Path]:
    """Collect the existing Python file-budget scope without following links."""
    files: set[Path] = set()
    visited: set[Path] = set()

    def visit(candidate: Path) -> None:
        if candidate in visited:
            return
        visited.add(candidate)
        try:
            children = directory_budget.entries(candidate)
        except NotADirectoryError:
            if candidate.suffix == ".py":
                files.add(candidate)
            return
        except OSError:
            return
        for entry in children:
            visit(entry)

    for target in targets:
        for scope in (repo_root / name for name in structure_dirs):
            selected = directory_budget.selected_under(target, scope, repo_root)
            if selected is not None:
                visit(selected)
    return files


def check(
    targets: list[Path],
    directory_targets: list[Path],
    repo_root: Path,
    structure_dirs: tuple[str, ...],
) -> list[str]:
    """Violations of both ceilings; raises on an unreadable Git listing."""
    files = budget_files(targets, repo_root, structure_dirs)
    errors: list[str] = []
    for path in sorted(files):
        text = read_utf8_text(path)
        if text is None:
            continue  # Preserve the shared lint contract for unreadable members.
        count = len(text.splitlines())
        name = path.relative_to(repo_root).as_posix()
        if count > HARD_CEILING:
            errors.append(
                f"{name}:{count}: file is {count} lines, over the {HARD_CEILING}-line hard ceiling — {_FILE_CEILING_GUIDANCE}"
            )
    children = directory_budget.tracked_children(GitRepo(repo_root).run_text("ls-files", "-z"))
    for name in sorted(
        directory_budget.selected_directories(children, directory_targets, repo_root)
    ):
        count = len(children[name])
        if count > DIRECTORY_CEILING:
            errors.append(
                f"{name}: directory has {count} direct entries, over the {DIRECTORY_CEILING}-entry cap — {_DIRECTORY_CEILING_GUIDANCE}"
            )
    return errors
