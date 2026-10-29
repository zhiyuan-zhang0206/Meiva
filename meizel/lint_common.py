from __future__ import annotations

import contextlib
import functools
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from meizel import is_repo_test_file, is_test_path, restrict, split_only
from meizel.checkout import roles_at, tracked_files
from meizel.graph.model import MeizelError
from meizel.roles import pytest_test_hosts, select_packages

__all__ = [
    "FRAMEWORK_DIRS",
    "admission",
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
# The governed production code, including independently packaged projects. Lints whose scope is
# "the framework" import this instead of keeping their own copy, so a new or renamed package cannot
# drift out of one lint's scope unnoticed. `meizel.roles` decides nothing about where a repository
# is, so this file reads the roles once, where it already owns the checkout dependency.
FRAMEWORK_DIRS = select_packages(roles_at(Path(__file__).parents[1]), "application", "standalone")


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
# One lint run admits the checkout's files once. A lint asks per target, and one run
# of `time_bomb` asked 21 times: each ask re-read git and re-checked every tracked
# path, which measured 75 s of a 92 s profiled run for three changed files. The reuse
# lives strictly inside `admission()`, so a caller outside the scope always reads the
# tree again and a file created between two runs is never missed — both halves are
# pinned in meizel/tests/test_lint_common.py. The exit prints one line, so "why slow
# one day and not the next" is answerable without a profiler.
class _Admission:
    def __init__(self) -> None:
        self.active = False
        self.files: dict[Path, list[str]] = {}
        self.files_count = 0
        self.reads = 0
        self.reuses = 0

    def _reset(self) -> None:
        self.active = False
        self.files.clear()
        self.files_count = 0
        self.reads = 0
        self.reuses = 0

    @contextlib.contextmanager
    def __call__(self) -> Iterator[None]:
        self._reset()
        self.active = True
        try:
            yield
        finally:
            if self.reads:
                print(
                    "lint admission: "
                    f"{self.files_count} files, {self.reads} read, {self.reuses} reused",
                    file=sys.stderr,
                )
            self._reset()

    def stats(self) -> dict[str, int]:
        return {"files": self.files_count, "reads": self.reads, "reuses": self.reuses}

    def scoped[**P](self, fn: Callable[P, int]) -> Callable[P, int]:
        @functools.wraps(fn)
        def run(*args: P.args, **kwargs: P.kwargs) -> int:
            with self():
                return fn(*args, **kwargs)

        return run


admission = _Admission()


# meiva:note
# Git owns source admission in a checkout: tracked regular files plus new nonignored
# regular files. Standalone environments are ignored dependencies, not governed source.
# A non-Git fixture keeps the explicit filesystem behavior of the lint API.
def _source_files(repo_root: Path) -> list[str] | None:
    if not _exists(repo_root / ".git"):
        return None
    if not admission.active:
        return sorted(tracked_files(repo_root, include_untracked=True, regular=True))
    admitted = admission.files.get(repo_root)
    if admitted is not None:
        admission.reuses += 1
        return admitted
    admitted = sorted(tracked_files(repo_root, include_untracked=True, regular=True))
    admission.files[repo_root] = admitted
    admission.reads += 1
    admission.files_count += len(admitted)
    return admitted


# meiva:note
# Select a directory's admitted files by name prefix. The admitted entries are
# repo-relative POSIX names, so `name.startswith(prefix + "/")` is the same set as
# resolving every name to a Path and asking `is_relative_to` — it measured 234k Path
# constructions (18 s of a 20 s profiled run) because one lint asked for its targets
# 21 times.
def _files_in_target(target: Path, repo_root: Path, admitted: list[str] | None) -> list[str]:
    if _is_file(target):
        return [_rel_or_abs(target, repo_root)]
    if _is_dir(target):
        if admitted is not None and target.is_relative_to(repo_root):
            prefix = target.relative_to(repo_root).as_posix().rstrip("/")
            below = prefix + "/" if prefix else ""
            return [name for name in admitted if name.startswith(below)]
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
