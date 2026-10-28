# meiva:note
# The effect shell of the two structure ceilings: it walks the checkout the file budget judges and
# reads every judged file, then asks `meizel.budgets.budget_plans` for the verdict. The ceilings,
# the exclusions, which paths and directories a target judges, what counts as a line or an entry and
# every word of the report are decided in that strict module.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

from pathlib import Path

from meizel.budgets import budget_plans, directory_budget
from meizel.graph.universe import GitRepo
from meizel.lint_common import read_utf8_text

__all__ = ["budget_files", "check"]


# meiva:note
# The existing Python files of the file budget's scope: each target narrowed to the structure
# directories, walked level by level and never through a link into a directory the budget does not
# enter.
def budget_files(
    targets: list[Path], repo_root: Path, structure_dirs: tuple[str, ...]
) -> set[Path]:
    files: set[Path] = set()
    visited: set[Path] = set()

    def visit(directory: Path) -> None:
        if directory in visited:
            return
        visited.add(directory)
        for entry in directory_budget.entries(directory):
            if entry.is_dir():
                visit(entry)
            elif entry.is_file() and budget_plans.is_python_file(entry.name):
                files.add(entry)

    for target in targets:
        for scope in (repo_root / name for name in structure_dirs):
            selected = directory_budget.selected_under(target, scope, repo_root)
            if selected is None:
                continue
            if selected.is_dir():
                visit(selected)
            elif selected.is_file() and budget_plans.is_python_file(selected.name):
                files.add(selected)
    return files


# meiva:note
# Both ceilings' violations for the given targets, read from one directory walk, one read per judged
# file and one Git listing of the tracked paths; a file that cannot be read keeps the shared lint
# contract of being skipped. Raises when Git cannot list the tree.
def check(
    targets: list[Path],
    directory_targets: list[Path],
    repo_root: Path,
    structure_dirs: tuple[str, ...],
) -> list[str]:
    texts: dict[Path, str] = {}
    for path in sorted(budget_files(targets, repo_root, structure_dirs)):
        text = read_utf8_text(path)
        if text is None:
            continue
        texts[path] = text
    children = budget_plans.tracked_children(GitRepo(repo_root).run_text("ls-files", "-z"))
    return budget_plans.ceiling_violations(
        texts,
        repo_root,
        budget_plans.judged_directories(children, directory_targets, repo_root),
    )
