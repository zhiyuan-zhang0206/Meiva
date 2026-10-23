"""Shared scan scope, file traversal and text decoding for standalone repository lints."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from meizel import is_repo_test_file, is_test_path, split_only
from meizel.roles import packages, pytest_test_hosts

__all__ = [
    "FRAMEWORK_DIRS",
    "changed_scope",
    "is_repo_test_file",
    "is_test_path",
    "pytest_test_hosts",
    "read_utf8_text",
    "resolve_targets",
    "restrict",
    "scan_roots",
    "split_only",
    "tracked_files",
]

# The framework code: every production Python package at the repo root. Lints
# whose scope is "the framework" import this instead of keeping their own copy,
# so a new or renamed package cannot drift out of one lint's scope unnoticed.
FRAMEWORK_DIRS = packages("application")


def scan_roots(repo_root: Path, dirs: Iterable[str]) -> list[Path]:
    """Resolve a lint's configured scan dirs; a missing one is a configuration error.

    A lint whose scope names a directory that does not exist checks nothing
    there and still exits 0, so the gap stays invisible. Raise instead.
    """
    roots = [repo_root / d for d in dirs]
    missing = [root.relative_to(repo_root).as_posix() for root in roots if not root.is_dir()]
    if missing:
        raise FileNotFoundError(
            f"lint scan dir(s) not found under {repo_root}: {', '.join(missing)}"
        )
    return roots


_LINT_TOOLING = (
    "meizel/",
    "meizel.policy.toml",
    "scripts/lint/",
    "scripts/structure/",
    "scripts/content_lint/",
    "scripts/lint/pool_keepalives.py",
)


def changed_scope(
    only: list[str] | None, repo_root: Path, *, inputs: Iterable[str] = ()
) -> frozenset[str] | None:
    """Repo-relative paths a `--only` run judges; None means judge everything.

    None without the flag, and when a changed path is part of the lint tooling or
    one of the lint's `inputs` (path prefixes): such an edit can change the verdict
    of files that were not touched.
    """
    if only is None:
        return None
    # lexists: a tracked symlink whose target is gone is still a path pre-commit hands us.
    missing = [a for a in only if not os.path.lexists(repo_root / a)]
    if missing:
        raise SystemExit(f"error: target path(s) not found: {', '.join(missing)}")
    changed = frozenset(_rel_or_abs((repo_root / a).absolute(), repo_root) for a in only)
    widening = (*_LINT_TOOLING, *inputs)
    if any(path.startswith(widening) and not is_test_path(path) for path in changed):
        return None  # a lint's own tests are not its tooling
    return changed


def restrict(paths: Iterable[Path], scope: frozenset[str] | None, repo_root: Path) -> list[Path]:
    """The enumerated files `scope` judges: all of them, or only the changed ones."""
    if scope is None:
        return list(paths)
    return [path for path in paths if _rel_or_abs(path, repo_root) in scope]


def tracked_files(repo_root: Path) -> list[str]:
    """Return git-tracked paths in git's order, relative to the repo root."""
    # Fixed command line; the root comes from the calling script's own __file__.
    out = subprocess.run(  # noqa: S603 - fixed argv, script-derived repo root
        ["git", "-C", str(repo_root), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return [path for path in out.stdout.splitlines() if path]


def _rel_or_abs(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolved_target(argument: str, repo_root: Path) -> Path:
    path = Path(argument)
    if not path.is_absolute():
        candidate = repo_root / path
        if candidate.exists():
            path = candidate
    return path.resolve()


def _files_in_target(target: Path, repo_root: Path) -> list[str]:
    if target.is_file():
        return [_rel_or_abs(target, repo_root)]
    if target.is_dir():
        return [_rel_or_abs(path, repo_root) for path in target.rglob("*") if path.is_file()]
    return []


def resolve_targets(argv: list[str], repo_root: Path) -> tuple[list[str], list[str]]:
    """Return sorted explicit files and any original arguments that are missing."""
    targets = [_resolved_target(argument, repo_root) for argument in argv]
    missing = [a for a, target in zip(argv, targets, strict=True) if not target.exists()]
    if missing:
        return [], missing

    files = [file for target in targets for file in _files_in_target(target, repo_root)]
    return sorted(set(files)), []


def read_utf8_text(path: Path) -> str | None:
    """Skip unreadable, binary, and non-UTF-8 files."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
