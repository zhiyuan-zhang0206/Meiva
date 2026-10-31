# meiva:note
# Run the checks one change needs and print the one line a pull request body carries. It belongs
# beside the pre-push hooks that approximate it today: `scripts/` is at its entry cap and
# `scripts/ci/` may not reach `scripts/audit` without closing a cycle.

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_VERSION = "diff-checks/v1"
_BIN = Path(sys.executable).parent
_COUNTS = re.compile(r"(\d+) (passed|failed|errors?)")


# meiva:note
# What one change looks like to a trigger: the repository root, the comparison base and the paths
# the change touches. A trigger reads this and decides whether its runs belong to this change.
@dataclass(frozen=True)
class ChangeSet:
    root: Path
    base: str
    files: tuple[str, ...]

    def python_files(self) -> tuple[str, ...]:
        return tuple(name for name in self.files if name.endswith(".py"))


# meiva:note
# One command a trigger asks for, named by the receipt field it fills. `judge` states how a reader
# decides red — an exit code, or a count against a threshold. The runner never reads output text
# for a verdict, so a check cannot turn green by printing the right word.
@dataclass(frozen=True)
class Run:
    label: str
    argv: tuple[str, ...]
    judge: str
    cwd: str = ""


@dataclass(frozen=True)
class Trigger:
    name: str
    matches: Callable[[ChangeSet], bool]
    runs: Callable[[ChangeSet], tuple[Run, ...]]
    blocking: bool


# meiva:note
# The fields of a receipt, in the order a reader meets them. A run fills its own field; the ones
# no run fills (`fallback`, `vitest`, `meizel`, `debt`) are printed as `none` until their trigger
# exists, so a missing check is visible instead of absent.
FIELD_ORDER = (
    "pyright",
    "err_in_this_diff",
    "ruff",
    "selector",
    "tests",
    "passed",
    "failed",
    "fallback",
    "blind",
    "vitest",
    "next-build",
    "meizel",
    "debt",
    "refusal-loss",
    "skipped",
)


# meiva:note
# The whole-tree pyright error count the base commit carries, measured with the command CI runs
# and in a worktree that owns its own venv: `basedpyright --threads 2` on `origin/main`
# 9bb1eda26 gave 0 errors in 96s. A tree whose venv is shared with another checkout reports
# invented errors (two identities for one class), so the number is only comparable when
# `python -c "import base; print(base.__file__)"` names this worktree.
PYRIGHT_ERROR_BASELINE = 0


def _tool(name: str) -> str:
    candidate = _BIN / name
    return str(candidate) if candidate.exists() else name


def _python_runs(change: ChangeSet) -> tuple[Run, ...]:
    return (
        Run(
            "pyright",
            (_tool("basedpyright"), "--threads", "2"),
            f"whole-tree errors must stay at the base's {PYRIGHT_ERROR_BASELINE}",
        ),
        Run("ruff", (_tool("ruff"), "check", *change.python_files()), "exit code"),
        Run("meizel", (sys.executable, "-m", "meizel", "check"), "exit code"),
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
# Whether a change reduces recorded debt: a `meizel.toml` whose deleted lines outnumber the
# added ones. The refusal-loss scan is the check that shape owes — a debt repaid by deleting a
# refusal has to say which refusal took its place.
def _net_debt_reduction(change: ChangeSet) -> bool:
    manifests = [name for name in change.files if name.endswith("meizel.toml")]
    if not manifests:
        return False
    counted = _git(change.root, "diff", "--numstat", f"{change.base}...HEAD", "--", *manifests)
    added = deleted = 0
    for line in counted.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            added += int(fields[0])
            deleted += int(fields[1])
    return deleted > added


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
            text = (change.root / name).read_text()
        except OSError:
            continue
        if any(marker in text for marker in _RSC_IMPORTS):
            return True
    return False


_FRONTEND_RUNS = (
    Run(
        "next-build",
        (shutil.which("npm") or "npm", "run", "build"),
        "exit code; the production build refuses a client boundary in a server component",
        cwd="ui/web",
    ),
)


# meiva:note
# The change shapes this script knows. `python-change` is the one every Python edit takes;
# other owners add theirs here (a net debt reduction's refusal-loss scan, for example) and the
# runner below only schedules what a trigger names.
TRIGGERS: tuple[Trigger, ...] = (
    Trigger(
        name="python-change",
        matches=lambda change: bool(change.python_files()),
        runs=_python_runs,
        blocking=True,
    ),
    Trigger(
        name="frontend-rsc",
        matches=_touches_rsc_boundary,
        runs=lambda _change: _FRONTEND_RUNS,
        blocking=True,
    ),
    Trigger(
        name="debt-reduced",
        matches=_net_debt_reduction,
        runs=_refusal_runs,
        blocking=True,
    ),
)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def changed_files(base: str, root: Path) -> tuple[str, ...]:
    listing = _git(root, "diff", "--name-only", f"{base}...HEAD")
    return tuple(line.strip() for line in listing.splitlines() if line.strip())


def _spell(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60}m{whole % 60:02d}s" if whole >= 60 else f"{whole}s"


# meiva:note
# The selector's decision for this change: the files it chose, whether it covered the change
# whole (`FULL`), and the changed files it admits it cannot map. `blind` is reported rather than
# swallowed — a blind change needs the fallback run that the receipt then names.
@dataclass(frozen=True)
class Selection:
    decision: str
    tests: tuple[str, ...]
    blind: tuple[str, ...]

    @property
    def field(self) -> str:
        return (
            f"BLIND({','.join(self.blind)})" if self.blind else f"{self.decision}:{len(self.tests)}"
        )


def select_tests(change: ChangeSet) -> Selection:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as listing:
        listing.write("\n".join(change.files) + "\n")
        listing_path = listing.name
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.ci.test_selector",
                "--changed-files",
                listing_path,
                "--base-ref",
                change.base,
                "--repo-root",
                str(change.root),
                "--json",
            ],
            cwd=change.root,
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        Path(listing_path).unlink(missing_ok=True)
    payload = json.loads(result.stdout)
    return Selection(
        decision=str(payload.get("decision", "UNKNOWN")),
        tests=tuple(payload.get("tests", ())),
        blind=tuple(str(name) for name in payload.get("blind_changed", ())),
    )


def _count(output: str, word: str) -> int:
    found = re.search(rf"(\d+) {word}\b", output)
    return int(found.group(1)) if found else 0


def _execute(argv: Sequence[str], root: Path) -> tuple[int, str, float]:
    started = time.monotonic()
    result = subprocess.run(list(argv), cwd=root, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout + result.stderr, time.monotonic() - started


def _pyright_errors(output: str) -> int:
    return int(found.group(1)) if (found := re.search(r"(\d+) errors?\b", output)) else 0


_ERROR_LINE = re.compile(r"^(?P<path>[^\s:][^:]*):\d+:\d+ - error:")


# meiva:note
# How many of the tree's errors sit in files this change touches. The total answers "is the
# tree green"; this answers "did I do it", and a receipt that only carried one of the two would
# let a reader mistake a local fact for a global one.
def _errors_in_diff(output: str, change: ChangeSet) -> int:
    touched = set(change.files)
    return sum(
        1
        for line in output.splitlines()
        if (found := _ERROR_LINE.match(line.strip()))
        and any(found.group("path").endswith(name) for name in touched)
    )


def _debt_field(output: str) -> str:
    header: list[str] = []
    rows: dict[str, list[str]] = {}
    for line in output.splitlines():
        cells = [cell.strip() for cell in re.split(r"\s{2,}", line.strip()) if cell.strip()]
        if not cells:
            continue
        if cells[0] == "code":
            header = cells
            continue
        rows[cells[0]] = cells[1:]
    if "origin/main" not in header or "tree" not in header:
        return "unreadable"
    base_column = header.index("origin/main") - 1
    tree_column = header.index("tree") - 1
    ambient = rows.get("ambient-read")
    total = rows.get("total")
    if ambient is None or total is None or len(total) <= tree_column:
        return "unreadable"
    return (
        f"ambient-read{ambient[base_column]}->{ambient[tree_column]},"
        f"total{total[base_column]}->{total[tree_column]}"
    )


def _refusal_field(output: str) -> str:
    found = re.search(r"refusal-loss: hits=(\d+) blocking=(\d+) unreplaced=(\d+)", output)
    if found is None:
        return "unreadable"
    return f"hits={found.group(1)},blocking={found.group(2)},unreplaced={found.group(3)}"


def _fields_for(
    run: Run, code: int, output: str, seconds: float, change: ChangeSet
) -> dict[str, str]:
    if run.label == "pyright":
        errors = _pyright_errors(output)
        over_baseline = errors > PYRIGHT_ERROR_BASELINE or code != 0
        return {
            "pyright": f"{errors}err({_spell(seconds)})",
            "err_in_this_diff": str(_errors_in_diff(output, change)),
            "_over_baseline": "1" if over_baseline else "0",
        }
    if run.label == "debt":
        return {"debt": _debt_field(output)}
    if run.label == "refusal-loss":
        return {"refusal-loss": _refusal_field(output)}
    verdict = "ok" if code == 0 else f"failed({code})"
    return {run.label: f"{verdict}({_spell(seconds)})"}


def check(
    change: ChangeSet,
    *,
    skip_tests: bool,
    execute: Callable[[Sequence[str], Path], tuple[int, str, float]] = _execute,
    choose: Callable[[ChangeSet], Selection] = select_tests,
) -> tuple[dict[str, str], bool]:
    matched = [trigger for trigger in TRIGGERS if trigger.matches(change)]
    values: dict[str, str] = {}
    failed = False
    selection = choose(change)
    values["selector"] = selection.field
    values["blind"] = ",".join(selection.blind) if selection.blind else "none"
    if selection.blind:
        values["fallback"] = "none"
    for trigger in matched:
        for run in trigger.runs(change):
            code, output, seconds = execute(
                run.argv, change.root / run.cwd if run.cwd else change.root
            )
            fields = _fields_for(run, code, output, seconds, change)
            over_baseline = fields.pop("_over_baseline", "0") == "1"
            values.update(fields)
            failed = failed or (trigger.blocking and (code != 0 or over_baseline))
    if skip_tests:
        values["tests"] = "SKIPPED(--skip-tests)"
        return values, failed
    if not selection.tests:
        values["tests"] = "SKIPPED(the selector chose none)"
        return values, failed
    values["tests"] = "RAN"
    code, output, seconds = execute(
        (sys.executable, "-m", "pytest", "-q", *selection.tests), change.root
    )
    values["passed"] = f"{_count(output, 'passed')}({_spell(seconds)})"
    values["failed"] = str(_count(output, "failed"))
    failed = failed or code != 0
    return values, failed


def build_receipt(values: dict[str, str], sha: str, base: str) -> str:
    fields = [f"{label}={values.get(label, 'none')}" for label in FIELD_ORDER]
    return " ".join(
        (
            f"{RECEIPT_VERSION} @{sha}",
            f"base={base}",
            *fields,
            "generated=scripts/hooks/diff_checks.py",
        )
    )


def plan(change: ChangeSet, *, skip_tests: bool) -> list[str]:
    matched = [trigger for trigger in TRIGGERS if trigger.matches(change)]
    lines = [f"change: {len(change.files)} files against {change.base}"]
    for trigger in matched:
        lines.append(f"trigger {trigger.name} (blocking={trigger.blocking})")
        for run in trigger.runs(change):
            where = f" (in {run.cwd})" if run.cwd else ""
            lines.append(f"  {run.label}: {' '.join(run.argv[:4])}{where} [{run.judge}]")
    lines.append("  selector: scripts/ci/test_selector.py --json")
    if not skip_tests:
        lines.append("  tests: the files the selector chooses")
    return lines


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="the commit to compare HEAD with")
    parser.add_argument(
        "--changed-files",
        type=Path,
        help="a newline-separated list to use instead of the git diff (re-running a past change)",
    )
    parser.add_argument("--skip-tests", action="store_true", help="select tests, run none of them")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and run nothing")
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    root = arguments.repo_root.resolve()
    files = (
        tuple(
            line.strip()
            for line in arguments.changed_files.read_text().splitlines()
            if line.strip()
        )
        if arguments.changed_files is not None
        else changed_files(arguments.base, root)
    )
    change = ChangeSet(root=root, base=arguments.base, files=files)
    sha = _git(root, "rev-parse", "HEAD")
    if not change.files:
        print(f"{RECEIPT_VERSION} @{sha} no-changes")
        return 0
    if arguments.dry_run:
        print("\n".join(plan(change, skip_tests=arguments.skip_tests)))
        return 0
    values, failed = check(change, skip_tests=arguments.skip_tests)
    print(build_receipt(values, sha, _git(root, "rev-parse", arguments.base)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
