from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from meizel import is_repo_test_file, is_test_path, restrict, split_only
from meizel.checkout import tracked_files
from meizel.graph.model import MeizelError
from meizel.roles import packages, pytest_test_hosts

__all__ = [
    "FRAMEWORK_DIRS",
    "changed_scope",
    "is_repo_test_file",
    "is_test_path",
    "pytest_test_hosts",
    "python_files",
    "read_utf8_text",
    "resolve_targets",
    "restrict",
    "run_node_analyzer",
    "scan_roots",
    "split_only",
    "tracked_files",
]

# meiva:note
# The governed production code, including independently packaged projects. Lints
# whose scope is "the framework" import this instead of keeping their own copy,
# so a new or renamed package cannot drift out of one lint's scope unnoticed.
FRAMEWORK_DIRS = packages("application", "standalone")


# meiva:note
# Check whether a candidate path on disk is a regular file.
def _is_file(path: Path) -> bool:
    return path.is_file()


# meiva:note
# Check whether a candidate path on disk is a directory.
def _is_dir(path: Path) -> bool:
    return path.is_dir()


# meiva:note
# Check whether a lint target path resolves to an existing file or directory.
def _exists(path: Path) -> bool:
    return _is_file(path) or _is_dir(path)


def scan_roots(repo_root: Path, dirs: Iterable[str]) -> list[Path]:
    """Resolve a lint's configured scan dirs; a missing one is a configuration error.

    A lint whose scope names a directory that does not exist checks nothing
    there and still exits 0, so the gap stays invisible. Raise instead.
    """
    roots = [repo_root / d for d in dirs]
    missing = [root.relative_to(repo_root).as_posix() for root in roots if not _is_dir(root)]
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


def _rel_or_abs(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolved_target(argument: str, repo_root: Path) -> Path:
    path = Path(argument)
    if not path.is_absolute():
        candidate = repo_root / path
        if _exists(candidate):
            path = candidate
    return path.resolve()


# meiva:note
# Git owns source admission in a checkout: tracked regular files plus new nonignored
# regular files. Standalone environments are ignored dependencies, not governed source.
# A non-Git fixture keeps the explicit filesystem behavior of the lint API.
def _source_files(repo_root: Path) -> list[str] | None:
    if not _exists(repo_root / ".git"):
        return None
    return sorted(tracked_files(repo_root, include_untracked=True, regular=True))


def _files_in_target(target: Path, repo_root: Path, admitted: list[str] | None) -> list[str]:
    if _is_file(target):
        return [_rel_or_abs(target, repo_root)]
    if _is_dir(target):
        if admitted is not None and target.is_relative_to(repo_root):
            return [name for name in admitted if (repo_root / name).is_relative_to(target)]
        return [_rel_or_abs(path, repo_root) for path in target.rglob("*") if _is_file(path)]
    return []


def resolve_targets(argv: list[str], repo_root: Path) -> tuple[list[str], list[str]]:
    """Return sorted explicit files and any original arguments that are missing."""
    targets = [_resolved_target(argument, repo_root) for argument in argv]
    missing = [a for a, target in zip(argv, targets, strict=True) if not _exists(target)]
    if missing:
        return [], missing
    admitted = _source_files(repo_root)
    files = [file for target in targets for file in _files_in_target(target, repo_root, admitted)]
    return sorted(set(files)), []


# meiva:note
# Python source below the chosen roots, using the same admission as explicit directory
# targets. Read Git once for the whole scan; explicit leaf files retain their audit meaning.
def python_files(roots: Iterable[Path], repo_root: Path) -> list[Path]:
    admitted = _source_files(repo_root)
    files = {
        file
        for target in roots
        for file in _files_in_target(target, repo_root, admitted)
        if file.endswith(".py")
    }
    return [repo_root / file for file in sorted(files)]


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


# meiva:note
# This I/O boundary starts one tool-owned analyzer with JSON stdin and bounded heap/time.
# Successful fact production is not a semantic purity proof of the scanned code.
# meiva:link meizel/docs/checker/typescript.meiva.okf.md
def run_node_analyzer(producer: Path, payload: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["node", "--max-old-space-size=4096", str(producer)],
            input=payload,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MeizelError(
            f"TypeScript compiler unavailable: {error}; install Node and the tool's locked ui/web dependencies"
        ) from error
    if result.returncode != 0:
        raise MeizelError(f"TypeScript compiler failed: {result.stderr.strip()}")
    return result.stdout
