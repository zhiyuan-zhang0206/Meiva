from __future__ import annotations

import functools
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from meizel import is_repo_test_file, is_test_path, restrict, split_only
from meizel.frontend_deps import FrontendDeps, FrontendDepsError, classify, observe, refusal
from meizel.graph.model import MeizelError
from meizel.graph.universe import SourceTree
from meizel.roles import pytest_test_hosts
from meizel.source_text import decode_source

__all__ = [
    "admission",
    "changed_scope",
    "decode_source",
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
]


# meiva:note
# Check whether the tree holds a regular file at `path`.
def _is_file(tree: SourceTree, path: Path) -> bool:
    return tree.is_file(path)


# meiva:note
# Check whether the tree holds a directory at `path`.
def _is_dir(tree: SourceTree, path: Path) -> bool:
    return tree.is_dir(path)


# meiva:note
# Check whether a lint target path resolves to an existing file or directory.
def _exists(tree: SourceTree, path: Path) -> bool:
    return _is_file(tree, path) or _is_dir(tree, path)


def scan_roots(tree: SourceTree, dirs: Iterable[str]) -> list[Path]:
    """Resolve a lint's configured scan dirs; a missing one is a configuration error.

    A lint whose scope names a directory that does not exist checks nothing
    there and still exits 0, so the gap stays invisible. Raise instead.
    """
    roots = [tree.root / d for d in dirs]
    missing = [root.relative_to(tree.root).as_posix() for root in roots if not _is_dir(tree, root)]
    if missing:
        raise FileNotFoundError(
            f"lint scan dir(s) not found under {tree.root}: {', '.join(missing)}"
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
    tree: SourceTree, only: list[str] | None, *, inputs: Iterable[str] = ()
) -> frozenset[str] | None:
    """Repo-relative paths a `--only` run judges; None means judge everything.

    None without the flag, and when a changed path is part of the lint tooling or
    one of the lint's `inputs` (path prefixes): such an edit can change the verdict
    of files that were not touched.
    """
    if only is None:
        return None
    # lexists: a tracked symlink whose target is gone is still a path pre-commit hands us.
    missing = [a for a in only if not tree.lexists(tree.root / a)]
    if missing:
        raise SystemExit(f"error: target path(s) not found: {', '.join(missing)}")
    changed = frozenset(_rel_or_abs((tree.root / a).absolute(), tree.root) for a in only)
    widening = (*_LINT_TOOLING, *inputs)
    if any(path.startswith(widening) and not is_test_path(path) for path in changed):
        return None  # a lint's own tests are not its tooling
    return changed


def _rel_or_abs(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolved_target(tree: SourceTree, argument: str) -> Path:
    path = Path(argument)
    if not path.is_absolute():
        candidate = tree.root / path
        if _exists(tree, candidate):
            path = candidate
    return tree.resolve(path)


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

    def __enter__(self) -> _Admission:
        self._reset()
        self.active = True
        return self

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
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
            with self:
                return fn(*args, **kwargs)

        return run


admission = _Admission()


# meiva:note
# Git owns source admission in a checkout: tracked regular files plus new nonignored
# regular files. Standalone environments are ignored dependencies, not governed source.
# A non-Git fixture keeps the explicit filesystem behavior of the lint API.
def _source_files(tree: SourceTree) -> list[str] | None:
    if not _exists(tree, tree.root / ".git"):
        return None
    if not admission.active:
        return sorted(tree.tracked(include_untracked=True, regular=True))
    admitted = admission.files.get(tree.root)
    if admitted is not None:
        admission.reuses += 1
        return admitted
    admitted = sorted(tree.tracked(include_untracked=True, regular=True))
    admission.files[tree.root] = admitted
    admission.reads += 1
    admission.files_count += len(admitted)
    return admitted


# meiva:note
# Select a directory's admitted files by name prefix. The admitted entries are
# repo-relative POSIX names, so `name.startswith(prefix + "/")` is the same set as
# resolving every name to a Path and asking `is_relative_to` — it measured 234k Path
# constructions (18 s of a 20 s profiled run) because one lint asked for its targets
# 21 times.
def _files_in_target(tree: SourceTree, target: Path, admitted: list[str] | None) -> list[str]:
    if _is_file(tree, target):
        return [_rel_or_abs(target, tree.root)]
    if _is_dir(tree, target):
        if admitted is not None and target.is_relative_to(tree.root):
            prefix = target.relative_to(tree.root).as_posix().rstrip("/")
            below = prefix + "/" if prefix else ""
            return [name for name in admitted if name.startswith(below)]
        return [
            _rel_or_abs(path, tree.root) for path in tree.rglob(target, "*") if _is_file(tree, path)
        ]
    return []


def resolve_targets(tree: SourceTree, argv: list[str]) -> tuple[list[str], list[str]]:
    """Return sorted explicit files and any original arguments that are missing."""
    targets = [_resolved_target(tree, argument) for argument in argv]
    missing = [a for a, target in zip(argv, targets, strict=True) if not _exists(tree, target)]
    if missing:
        return [], missing
    admitted = _source_files(tree)
    files = [file for target in targets for file in _files_in_target(tree, target, admitted)]
    return sorted(set(files)), []


# meiva:note
# Python source below the chosen roots, using the same admission as explicit directory
# targets. Read Git once for the whole scan; explicit leaf files retain their audit meaning.
def python_files(tree: SourceTree, roots: Iterable[Path]) -> list[Path]:
    admitted = _source_files(tree)
    files = {
        file
        for target in roots
        for file in _files_in_target(tree, target, admitted)
        if file.endswith(".py")
    }
    return [tree.root / file for file in sorted(files)]


# meiva:note
# The reader around `decode_source`: the one place that pays for the bytes. A lint whose rule
# already holds the content calls the decision, not this.
def read_utf8_text(tree: SourceTree, path: Path) -> str | None:
    try:
        data = tree.read_bytes(path)
    except OSError:
        return None
    return decode_source(data)


# meiva:note
# Node reports a missing analyzer dependency as a module-not-found stack raised from the package dir,
# which never says that the *checkout* lacks the dependencies.
# Name the fix instead of the stack.
def _missing_frontend_dependencies(stderr: str) -> str | None:
    if "Cannot find module 'typescript'" not in stderr and "MODULE_NOT_FOUND" not in stderr:
        return None
    return (
        "this checkout has no installed frontend dependencies, so the analyzer "
        "cannot load the `typescript` the lockfile pins. Run `bash scripts/setup-worktree.sh` "
        "inside this worktree, or run `pnpm install --frozen-lockfile` in the frontend directory"
    )


# meiva:note
# This I/O boundary starts one tool-owned analyzer with JSON stdin and bounded heap/time.
# Successful fact production is not a semantic purity proof of the scanned code.
# meiva:link meizel/docs/frontend-deps.meiva.okf.md
def run_node_analyzer(
    tree: SourceTree, producer: Path, payload: str, *, frontend: Path | None = None
) -> str:
    # meiva:note
    # A frontend tree this checkout did not install from its own lockfile answers for a graph the
    # lockfile does not describe, so the refusal happens before node starts and names the fix.
    # meiva:link meizel/docs/frontend-deps.meiva.okf.md
    if frontend is not None:
        state = classify(observe(tree, frontend))
        if state is not FrontendDeps.READY:
            raise FrontendDepsError(refusal(state))
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
            f"TypeScript compiler unavailable: {error}; install Node and the tool's locked package dependencies"
        ) from error
    if result.returncode != 0:
        hint = _missing_frontend_dependencies(result.stderr)
        if hint is not None:
            raise MeizelError(f"TypeScript compiler failed: {hint}")
        raise MeizelError(f"TypeScript compiler failed: {result.stderr.strip()}")
    return result.stdout
