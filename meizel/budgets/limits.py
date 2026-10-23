"""The 800-line file ceiling and the 20-entry directory cap."""

from __future__ import annotations

import subprocess
from pathlib import Path

from meizel.budgets import directory_budget

__all__ = ["DIRECTORY_CEILING", "HARD_CEILING", "budget_files", "check"]

HARD_CEILING = 800
DIRECTORY_CEILING = 20
_GENERATED_TS_SUFFIXES = (".d.ts", "-generated.ts", "-generated.tsx")
_FILE_CEILING_GUIDANCE = (
    "800 lines is a generous budget for a single leaf Deep Module hiding rich internal logic "
    "behind a small public surface (<= 6 symbols); exceeding it means this file has outgrown a "
    "single leaf module — step back and view the package holistically: separate pure decision "
    "logic from I/O orchestration, or graduate the module into a cohesive subpackage directory "
    "behind a narrow `meizel.toml` `exports` door (<= 4 exports) so tree depth absorbs growth "
    "and local reasoning stays O(1), rather than mechanical `_part2` / `_helpers` slicing"
)
_DIRECTORY_CEILING_GUIDANCE = (
    "the 20-entry ceiling bounds flat branching factor at each single node while directory "
    "tree depth is unbounded (no cap on total codebase size) — think globally about the domain "
    "taxonomy and group related modules into cohesive subpackages with narrow `meizel.toml` "
    "`exports` doors (<= 4 exports) so every directory remains an O(1) navigable map of concepts"
)


def _is_governed_ts(path: Path) -> bool:
    return path.suffix in {".ts", ".tsx"} and not path.name.endswith(_GENERATED_TS_SUFFIXES)


def _matches_budget_file(entry: Path, *, ts_only: bool) -> bool:
    return _is_governed_ts(entry) if ts_only else (entry.suffix == ".py" or _is_governed_ts(entry))


def _target_scopes(
    target: Path, repo_root: Path, structure_dirs: tuple[str, ...]
) -> list[tuple[Path, bool]]:
    ui_scope = repo_root / "ui"
    scopes = [(repo_root / name, name == "ui") for name in structure_dirs]
    if "ui" not in structure_dirs and (target == ui_scope or ui_scope in target.parents):
        scopes.append((ui_scope, True))
    return scopes


def budget_files(
    targets: list[Path], repo_root: Path, structure_dirs: tuple[str, ...]
) -> set[Path]:
    """Collect the existing Python file-budget scope without following links."""
    files: set[Path] = set()
    visited: set[tuple[Path, bool]] = set()

    def collect(selected: Path, *, ts_only: bool) -> None:
        if selected.is_file():
            if _matches_budget_file(selected, ts_only=ts_only):
                files.add(selected)
            return
        if not selected.is_dir() or (selected, ts_only) in visited:
            return
        visited.add((selected, ts_only))
        for entry in directory_budget.entries(selected):
            collect(entry, ts_only=ts_only)

    for target in targets:
        for scope, ts_only in _target_scopes(target, repo_root, structure_dirs):
            if (selected := directory_budget.selected_under(target, scope, repo_root)) is not None:
                collect(selected, ts_only=ts_only)
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
        try:
            count = len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue  # Preserve the shared lint contract for unreadable members.
        name = path.relative_to(repo_root).as_posix()
        if count > HARD_CEILING:
            errors.append(
                f"{name}:{count}: file is {count} lines, over the {HARD_CEILING}-line hard ceiling — {_FILE_CEILING_GUIDANCE}"
            )
    listing = subprocess.run(  # noqa: S603 — local git query, no shell
        ["git", "-C", str(repo_root), "ls-files", "-z"], capture_output=True, text=True, check=False
    )
    listing.check_returncode()
    children = directory_budget.tracked_children(listing.stdout)
    for name in sorted(
        directory_budget.selected_directories(children, directory_targets, repo_root)
    ):
        count = len(children[name])
        if count > DIRECTORY_CEILING:
            errors.append(
                f"{name}: directory has {count} direct entries, over the {DIRECTORY_CEILING}-entry cap — {_DIRECTORY_CEILING_GUIDANCE}"
            )
    return errors
