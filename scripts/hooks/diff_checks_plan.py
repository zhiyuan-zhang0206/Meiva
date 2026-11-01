# meiva:note
# Build the pure execution plan (`ScheduledGroup` of `Run` commands) for a `ChangeSet` in either
# `incremental` or `full` mode. Kept free of subprocess execution and environment reads so plan
# construction stays pure data-in, plan-out.

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from scripts.hooks.diff_checks_cache import (
    CacheContext,
    CheckMode,
    lookup_closure,
    record_closure,
)
from scripts.hooks.diff_checks_fields import ChangeSet, Run, kinds, reverse_closure, scope_of

# meiva:note
# The whole-tree pyright error count the base commit carries, measured with the command CI runs
# and in a worktree that owns its own venv: `basedpyright --threads 2` on `origin/main`
# 9bb1eda26 gave 0 errors in 96s. A tree whose venv is shared with another checkout reports
# invented errors (two identities for one class), so the number is only comparable when
# `python -c "import base; print(base.__file__)"` names this worktree.
PYRIGHT_ERROR_BASELINE = 0

_BIN = Path(sys.executable).parent
_GLOBAL_TYPE_CONFIGS = frozenset({"pyproject.toml", "pyrightconfig.json", "uv.lock"})


@dataclass(frozen=True)
class ScheduledGroup:
    name: str
    runs: tuple[Run, ...]
    blocking: bool = True


def _tool(name: str) -> str:
    candidate = _BIN / name
    return str(candidate) if candidate.exists() else name


def _global_type_config_changed(change: ChangeSet) -> bool:
    return any(name in _GLOBAL_TYPE_CONFIGS or name.startswith("typings/") for name in change.files)


def _resolved_closure(change: ChangeSet, cache: CacheContext | None) -> set[str] | None:
    python_files = change.python_files()
    if cache is not None:
        cached = lookup_closure(cache, python_files)
        if cached is not None:
            return cached
    reached = reverse_closure(change.root, change.files)
    if cache is not None and reached is not None:
        record_closure(cache, python_files, reached)
    return reached


def _incremental_pyright_runs(
    change: ChangeSet, *, cache: CacheContext | None = None
) -> tuple[Run, ...]:
    if not _global_type_config_changed(change) and change.root.is_dir():
        reached = _resolved_closure(change, cache)
        if reached is not None:
            targets = tuple(
                sorted(
                    path
                    for path in (reached | set(change.python_files()))
                    if (change.root / path).is_file()
                )
            )
            if not targets:
                return ()
            return (
                Run(
                    "pyright",
                    (_tool("basedpyright"), "--threads", "2", *targets),
                    "errors in the changed files and their reverse-import closure must stay 0",
                ),
            )
    return (
        Run(
            "pyright",
            (_tool("basedpyright"), "--threads", "2"),
            f"whole-tree errors must stay at the base's {PYRIGHT_ERROR_BASELINE}",
        ),
    )


def _incremental_ruff_runs(change: ChangeSet) -> tuple[Run, ...]:
    python_paths = (
        tuple(path for path in change.python_files() if (change.root / path).is_file())
        if change.root.is_dir()
        else change.python_files()
    )
    if not python_paths:
        return ()
    return (Run("ruff", (_tool("ruff"), "check", *python_paths), "exit code"),)


def _python_runs(change: ChangeSet, *, cache: CacheContext | None = None) -> tuple[Run, ...]:
    return (*_incremental_pyright_runs(change, cache=cache), *_incremental_ruff_runs(change))


def _full_python_runs() -> tuple[Run, ...]:
    return (
        Run(
            "pyright",
            (_tool("basedpyright"), "--threads", "2"),
            f"whole-tree errors must stay at the base's {PYRIGHT_ERROR_BASELINE}",
        ),
        Run("ruff", (_tool("ruff"), "check"), "exit code"),
    )


def _can_scope_meizel(change: ChangeSet) -> bool:
    if not change.files or not change.root.is_dir():
        return False
    return not any(
        name.endswith("meizel.toml")
        or name == "meizel.policy.toml"
        or name.startswith("meizel/")
        or not (change.root / name).is_file()
        for name in change.files
    )


# meiva:note
# The checker reads manifests, role lists and declarations for every change, not only a Python
# one: a documentation-only pull request can still leave a stale export or a role list behind.
# Incremental mode scopes facts to `--changed` when no manifest, policy or meizel source moved and
# every changed path still exists on disk; otherwise it runs the whole-tree check.
def _meizel_runs(change: ChangeSet) -> tuple[Run, ...]:
    if _can_scope_meizel(change):
        return (
            Run(
                "meizel",
                (sys.executable, "-m", "meizel", "check", "--changed", *change.files),
                "exit code",
            ),
        )
    return _full_meizel_runs()


def _full_meizel_runs() -> tuple[Run, ...]:
    return (Run("meizel", (sys.executable, "-m", "meizel", "check"), "exit code"),)


# meiva:note
# A recorded total moves only when a manifest moves: a new violation is what `meizel check` already
# refuses, and a repayment deletes the line that recorded it. Comparing against the base costs
# minutes, so it runs for a manifest change and says why it did not for any other.
def _debt_runs(change: ChangeSet) -> tuple[Run, ...]:
    return (
        Run(
            "debt",
            (
                sys.executable,
                "-m",
                "meizel",
                "debt",
                "diff",
                "--against",
                change.base,
                "--on-growth",
                "warn",
            ),
            "exit code; the receipt prints the ambient-read and total rows",
        ),
    )


# meiva:note
# A documentation change gets the document checks, not a type check it cannot fail: what a doc
# asserts is that its links, flags and nodes exist.
def _docs_runs() -> tuple[Run, ...]:
    return (
        Run(
            "docs-refs",
            (sys.executable, "scripts/content_lint/check_doc_references.py"),
            "exit code; the summary line carries the docs it read",
        ),
        Run(
            "docs-okf",
            (sys.executable, "scripts/content_lint/lint_meiva_okf.py"),
            "exit code; the summary line carries the documents it read",
        ),
    )


def _refusal_runs(change: ChangeSet) -> tuple[Run, ...]:
    return (
        Run(
            "refusal-loss",
            (
                sys.executable,
                "-m",
                "scripts.audit.refusal_loss",
                "--base",
                change.base,
                "--receipt",
            ),
            "exit code (a pure-deletion hunk); the line names hits/blocking/unreplaced",
        ),
    )


# meiva:note
# The React Server Component boundary is a framework rule: a Server Component that calls a client
# hook type-checks, lints and unit-tests clean, and only the production build refuses it. A change
# that touches the app router, or edits a file that imports next/navigation or next/headers, or
# that carries "use client", can move code across that boundary.
_RSC_PATHS = ("ui/web/src/app/",)
_RSC_IMPORTS = ("next/navigation", "next/headers", '"use client"')


def _touches_rsc_boundary(change: ChangeSet) -> bool:
    for name in change.files:
        if any(name.startswith(prefix) for prefix in _RSC_PATHS):
            return True
        if not name.endswith((".ts", ".tsx")):
            continue
        try:
            text = (change.root / name).read_text(encoding="utf-8")
        except OSError:
            continue
        if any(marker in text for marker in _RSC_IMPORTS):
            return True
    return False


_FRONTEND_RUNS = (
    Run(
        "next-build",
        (shutil.which("pnpm") or "pnpm", "run", "build"),
        "exit code; the production build refuses a client boundary in a server component",
        cwd="ui/web",
    ),
)

# meiva:note
# The selector never chooses every file: two page tests under `src/app` sat red from 2026-10-27 to
# 10-31 because no run ever picked them. Any change to the web tree runs the whole app router test
# tree (331 tests, 6.9s measured on the MacBook Air), so a file no selector names still gets one
# run. It is wider than `frontend-rsc` on purpose: the build decides whether code crosses a
# boundary, this decides whether the pages that already exist still pass.
_WEB_TREE = ("ui/web/",)


def _touches_web_tree(change: ChangeSet) -> bool:
    return any(name.startswith(_WEB_TREE) for name in change.files)


_APP_TEST_RUNS = (
    Run(
        "vitest-app",
        (
            shutil.which("npx") or "npx",
            "vitest",
            "run",
            "--config",
            "tests/vitest.config.mts",
            "--dir",
            "src/app",
        ),
        "exit code; every test under src/app, which the selector never chooses whole",
        cwd="ui/web",
    ),
)


def schedule_groups(
    change: ChangeSet,
    *,
    mode: CheckMode,
    debt_reduced: bool,
    cache: CacheContext | None = None,
) -> tuple[ScheduledGroup, ...]:
    if mode == "incremental":
        groups: list[ScheduledGroup] = []
        changed_kinds = kinds(change)
        if change.files:
            groups.append(ScheduledGroup("repository-change", _meizel_runs(change)))
        if change.python_files():
            groups.append(ScheduledGroup("python-change", _python_runs(change, cache=cache)))
        if "manifests" in changed_kinds:
            groups.append(ScheduledGroup("manifest-change", _debt_runs(change)))
        if "docs" in changed_kinds:
            groups.append(ScheduledGroup("docs-change", _docs_runs()))
        if _touches_rsc_boundary(change):
            groups.append(ScheduledGroup("frontend-rsc", _FRONTEND_RUNS))
        if _touches_web_tree(change):
            groups.append(ScheduledGroup("frontend-app", _APP_TEST_RUNS))
        if debt_reduced:
            groups.append(ScheduledGroup("debt-reduced", _refusal_runs(change)))
        return tuple(groups)
    if mode == "full":
        groups = [
            ScheduledGroup("full-meizel", _full_meizel_runs()),
            ScheduledGroup("full-python", _full_python_runs()),
            ScheduledGroup("full-debt", _debt_runs(change)),
            ScheduledGroup("full-docs", _docs_runs()),
            ScheduledGroup("full-frontend-rsc", _FRONTEND_RUNS),
            ScheduledGroup("full-frontend-app", _APP_TEST_RUNS),
        ]
        if debt_reduced:
            groups.append(ScheduledGroup("debt-reduced", _refusal_runs(change)))
        return tuple(groups)
    assert_never(mode)


def render_plan(
    change: ChangeSet,
    groups: tuple[ScheduledGroup, ...],
    *,
    skip_tests: bool,
    mode: CheckMode,
) -> list[str]:
    lines = [
        f"change: {len(change.files)} files against {change.base} "
        f"scope={scope_of(change)} mode={mode}"
    ]
    for group in groups:
        lines.append(f"trigger {group.name} (blocking={group.blocking})")
        for run in group.runs:
            where = f" (in {run.cwd})" if run.cwd else ""
            lines.append(f"  {run.label}: {' '.join(run.argv[:5])}{where} [{run.judge}]")
    lines.append("  selector: scripts/ci/test_selector.py --json (the files it chooses)")
    if not skip_tests:
        lines.append("  tests: the files the selector chooses")
    return lines
