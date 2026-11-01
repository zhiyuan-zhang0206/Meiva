# meiva:note
# Run the checks one change needs and print the one line a pull request body carries. It belongs
# beside the pre-push hooks that approximate it today: `scripts/` is at its entry cap and
# `scripts/ci/` may not reach `scripts/audit` without closing a cycle.

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# meiva:note
# A standalone script runs with its own directory off `sys.path`, so the repository root is put
# back before the one sibling this file imports: the closure scan, which answers a different
# question from scheduling and grew past the file budget here.
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.hooks.diff_closure import closure  # noqa: E402

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
# What kinds of file a change touches. The receipt prints it (`scope=`), a trigger reads it, and
# the layers nothing ran take their reason from it: a documentation-only change still owes the
# cheap document checks, and a Python change must not pay for a debt comparison it cannot move.
_KIND_SUFFIXES = (("docs", ".md"), ("manifests", "meizel.toml"), ("python", ".py"))


def _kinds(change: ChangeSet) -> frozenset[str]:
    kinds = {
        kind for kind, suffix in _KIND_SUFFIXES for name in change.files if name.endswith(suffix)
    }
    if any(name.startswith("ui/web/") for name in change.files):
        kinds.add("frontend")
    return frozenset(kinds)


def _scope(change: ChangeSet) -> str:
    kinds = sorted(_kinds(change))
    return "+".join(kinds) if kinds else "other"


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
# The fields of a receipt, in the order a reader meets them, and the whole vocabulary a value may
# use: the count a layer measured, `n/a(<why>)` for a check this change cannot need, `skipped(<why>)`
# for one it needs and did not run, and `unreadable(<why>)` for output the runner could not read.
# There is no bare `none` anywhere: "did not apply" and "ran, found nothing" must never read the
# same to the person merging on this line. No value carries a space -- the line is split on
# whitespace, so a reason joins its words with `-`.
FIELD_ORDER = (
    "scope",
    "pyright",
    "err_in_this_diff",
    "closure",
    "ruff",
    "selector",
    "tests",
    "passed",
    "failed",
    "errors",
    "blind",
    "fallback",
    "vitest-app",
    "next-build",
    "meizel",
    "debt",
    "refusal-loss",
    "docs-refs",
    "docs-okf",
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
    targets = tuple(
        f for f in change.python_files() if not change.root.exists() or (change.root / f).exists()
    )
    pyright_judge = f"whole-tree errors must stay at the base's {PYRIGHT_ERROR_BASELINE}"
    return (
        Run("pyright", (_tool("basedpyright"), "--threads", "2"), pyright_judge),
        Run("ruff", (_tool("ruff"), "check", *targets), "exit code"),
    )


# meiva:note
# The checker reads manifests, role lists and declarations for every change, not only a Python
# one: a documentation-only pull request can still leave a stale export or a role list behind.
def _meizel_runs(_change: ChangeSet) -> tuple[Run, ...]:
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
def _docs_runs(_change: ChangeSet) -> tuple[Run, ...]:
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


# meiva:note
# Whether a change reduces recorded debt: a `meizel.toml` whose deleted lines outnumber the
# added ones. The refusal-loss scan is the check that shape owes — a debt repaid by deleting a
# refusal has to say which refusal took its place.
def _net_debt_reduction(change: ChangeSet) -> bool:
    manifests = [name for name in change.files if name.endswith("meizel.toml")]
    if not manifests:
        return False
    # meiva:note
    # A manifest diff this runner cannot read is not evidence that nothing was repaid, so the
    # refusal scan runs: the check costs seconds and a missing scan is the expensive mistake.
    try:
        counted = _git(change.root, "diff", "--numstat", f"{change.base}...HEAD", "--", *manifests)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return True
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


# meiva:note
# The change shapes this script knows. `python-change` is the one every Python edit takes;
# other owners add theirs here (a net debt reduction's refusal-loss scan, for example) and the
# runner below only schedules what a trigger names.
TRIGGERS: tuple[Trigger, ...] = (
    Trigger(
        name="repository-change",
        matches=lambda change: bool(change.files),
        runs=_meizel_runs,
        blocking=True,
    ),
    Trigger(
        name="python-change",
        matches=lambda change: bool(change.python_files()),
        runs=_python_runs,
        blocking=True,
    ),
    Trigger(
        name="manifest-change",
        matches=lambda change: "manifests" in _kinds(change),
        runs=_debt_runs,
        blocking=True,
    ),
    Trigger(
        name="docs-change",
        matches=lambda change: "docs" in _kinds(change),
        runs=_docs_runs,
        blocking=True,
    ),
    Trigger(
        name="frontend-rsc",
        matches=_touches_rsc_boundary,
        runs=lambda _change: _FRONTEND_RUNS,
        blocking=True,
    ),
    Trigger(
        name="frontend-app",
        matches=_touches_web_tree,
        runs=lambda _change: _APP_TEST_RUNS,
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

    # meiva:note
    # A blind decision names the files it could not map; a decided one carries its unit, because
    # `SELECTED:152` beside `passed=3618` reads as two counts of the same thing.
    @property
    def field(self) -> str:
        return (
            f"BLIND({','.join(self.blind)})"
            if self.blind
            else f"{self.decision}:{len(self.tests)}files"
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


# meiva:note
# The `N errors` count in a tool's summary, in both senses this runner reads it. pytest counts a
# test that could not even start as an error and never as a failure: `31151 passed, 538 errors` is
# a run in which 538 tests did not happen while `failed` stayed 0, so a receipt that parsed only
# the failure count printed `failed=0tests` for a run whose shell exited 1 (#8001, 2026-11-01: 2267
# files, 538 errors). pyright states its findings the same way (`9 errors, 0 warnings`). Each is
# its own field so a reader can tell "nothing failed" from "much never ran".
def _error_count(output: str) -> int:
    found = re.search(r"(\d+) errors?\b", output)
    return int(found.group(1)) if found else 0


# meiva:note
# What a judged child inherits: the launcher's environment, minus the product's own namespace.
# The receipt exists to predict the verdict CI will give this change, and CI's runner carries no
# `MEIVA_*` at all — the suite installs the sentinels it needs before any project import
# (`tests/fixtures/env_bootstrap.py`) — while an agent host exports a hundred of them. They are
# not inert: the suite's session upgrade drops every cluster-scope key its own `.env` does not
# declare (`base/host/env/dotenv_boot.py:enforce_cluster_env_authority`), so a key the baseline
# snapshot still holds is gone from the live environment by the time a test compares the two, and
# `tests/harness/test_home_isolation.py` reds about the operator's shell rather than the change
# (#5907). Nothing else is filtered, deliberately: PATH, HOME, TMPDIR, the virtualenv and each
# toolchain's own variables must locate THIS box's tools, because CI's paths do not exist here —
# an allowlist would trade this false red for a new one nobody predicted.
def child_environment(parent: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in parent.items() if not name.startswith("MEIVA_")}


def run_child(
    argv: Sequence[str], root: Path, *, environment: Mapping[str, str]
) -> tuple[int, str, float]:
    started = time.monotonic()
    result = subprocess.run(
        list(argv),
        cwd=root,
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr, time.monotonic() - started


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


# meiva:note
# What the checker claimed: how many manifests and fact edges it read, and the exit code. A bare
# `ok` hides the difference between "covered the tree" and "found nothing to look at".
def _meizel_field(code: int, output: str, seconds: float) -> str:
    edges = re.search(r"(\d+) fact edges", output)
    manifests = re.search(r"(\d+) manifests", output)
    parts = [f"exit={code}"]
    if manifests is not None:
        parts.append(f"{manifests.group(1)}manifests")
    if edges is not None:
        parts.append(f"{edges.group(1)}edges")
    return f"{'ok' if code == 0 else 'failed'}({','.join(parts)})({_spell(seconds)})"


def _debt_field(code: int, output: str, seconds: float) -> str:
    verdict = "ok" if code == 0 else f"failed({code})"
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
    # meiva:note
    # Read the columns by position, never by the reference's own name: the first column after
    # `code` is whatever `--against` was given, which for a replay or a pull request base is a sha
    # or `<sha>~1`, and the column labelled `tree` is what this tree records. A change that edits a
    # rule or a declaration prints one more column before `tree`, which the label absorbs.
    if not header or header[0] != "code" or "tree" not in header:
        return f"unreadable(no-table)({_spell(seconds)})"
    tree_column = header.index("tree") - 1
    ambient = rows.get("ambient-read")
    total = rows.get("total")
    if ambient is None or total is None or len(total) <= tree_column or len(ambient) <= tree_column:
        return f"unreadable(no-totals)({_spell(seconds)})"
    return (
        f"{verdict}(ambient-read{ambient[0]}->{ambient[tree_column]},"
        f"total{total[0]}->{total[tree_column]})({_spell(seconds)})"
    )


# meiva:note
# The document checks state how much they read; a clean run that names only itself would leave a
# reader unable to tell a scan of three files from a scan of the tree.
def _docs_refs_field(code: int, output: str, seconds: float) -> str:
    verdict = "ok" if code == 0 else f"failed({code})"
    found = re.search(r"(\d+) docs checked, (\d+) dangling reference", output)
    if found is None:
        return f"unreadable(no-summary)({_spell(seconds)})"
    return f"{verdict}({found.group(1)}docs,{found.group(2)}dangling)({_spell(seconds)})"


def _docs_okf_field(code: int, output: str, seconds: float) -> str:
    verdict = "ok" if code == 0 else f"failed({code})"
    found = re.search(r"(\d+) files checked, (\d+) error\(s\), (\d+) warning\(s\)", output)
    if found is None:
        return f"unreadable(no-summary)({_spell(seconds)})"
    return (
        f"{verdict}({found.group(1)}files,{found.group(2)}errors,"
        f"{found.group(3)}warnings)({_spell(seconds)})"
    )


def _refusal_field(output: str) -> str:
    found = re.search(r"refusal-loss: hits=(\d+) blocking=(\d+) unreplaced=(\d+)", output)
    if found is None:
        return "unreadable"
    return f"hits={found.group(1)},blocking={found.group(2)},unreplaced={found.group(3)}"


# meiva:note
# A clean tree says so in one field and stops: no closure scan, no attribution. A red tree is
# attributed to this change only when the errors sit in files that can see it; anything the scan
# cannot prove stays inside, because "the tool could not tell" and "not my code" must not look
# the same in a receipt a reader trusts.
def _pyright_fields(output: str, seconds: float, change: ChangeSet) -> dict[str, str]:
    errors = _error_count(output)
    if errors == 0:
        return {
            "pyright": f"0err({_spell(seconds)})",
            "err_in_this_diff": str(_errors_in_diff(output, change)),
        }
    reached = closure(change.root, change.files)
    if reached is None:
        return {
            "pyright": f"{errors}err({_spell(seconds)})",
            "closure": "unknown",
            "_over_baseline": "1",
        }
    inside: list[str] = []
    outside: list[str] = []
    changed = set(change.files)
    root = str(change.root) + "/"
    for line in output.splitlines():
        found = _ERROR_LINE.match(line.strip())
        if found is None:
            continue
        name = found.group("path").removeprefix(root)
        matched = next((path for path in reached if name == path), None)
        if matched is not None or name in changed:
            inside.append(name)
        else:
            outside.append(name)
    distinct = sorted(set(outside))
    shown = ",".join(distinct[:3])
    more = len(distinct) - 3
    tail = f"{shown},+{more}more" if more > 0 else shown or "none"
    return {
        "pyright": (
            f"{errors}err({len(inside)}in-closure,{len(outside)}elsewhere:{tail})"
            f"({_spell(seconds)})"
        ),
        "err_in_this_diff": str(_errors_in_diff(output, change)),
        "_over_baseline": "1" if inside else "0",
    }


# meiva:note
# Vitest prints two counts: the files it read and the tests it answered. Only the second says how
# many assertions ran — `Test Files 34 passed` above `Tests 331 passed` is why a first-match regex
# would have reported 34. Its colour codes sit between the indent and the word `Tests`, so a run
# with colour on has to be stripped before the line can be read at all.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_VITEST_TESTS = re.compile(r"^\s*Tests\s+(?:(\d+) failed \| )?(\d+) passed", re.MULTILINE)


def _vitest_tests(output: str) -> int:
    found = _VITEST_TESTS.search(_ANSI.sub("", output))
    if found is None:
        return 0
    failed = int(found.group(1)) if found.group(1) else 0
    return failed + int(found.group(2))


def _fields_for(
    run: Run, code: int, output: str, seconds: float, change: ChangeSet
) -> dict[str, str]:
    if run.label == "pyright":
        return _pyright_fields(output, seconds, change)
    if run.label == "meizel":
        return {"meizel": _meizel_field(code, output, seconds)}
    if run.label == "vitest-app":
        ran = _vitest_tests(output)
        verdict = "ok" if code == 0 else f"failed({code})"
        return {"vitest-app": f"{verdict}({ran}tests)({_spell(seconds)})"}
    if run.label == "debt":
        return {"debt": _debt_field(code, output, seconds)}
    if run.label == "docs-refs":
        return {"docs-refs": _docs_refs_field(code, output, seconds)}
    if run.label == "docs-okf":
        return {"docs-okf": _docs_okf_field(code, output, seconds)}
    if run.label == "refusal-loss":
        return {"refusal-loss": _refusal_field(output)}
    verdict = "ok" if code == 0 else f"failed({code})"
    return {run.label: f"{verdict}({_spell(seconds)})"}


# meiva:note
# Why every layer no trigger ran is absent, said in this change's own terms: "no python touched"
# is a fact about the change, not an excuse, and the reader can check it against `scope=`.
def _not_run(change: ChangeSet) -> dict[str, str]:
    unread = {
        "pyright": "n/a(no-python-touched)",
        "err_in_this_diff": "n/a(no-python-touched)",
        "closure": "n/a(no-python-touched)",
        "ruff": "n/a(no-python-touched)",
        "vitest-app": "n/a(no-web-tree-file)",
        "next-build": "n/a(not-the-app-router)",
        "debt": "n/a(no-manifest-changed)",
        "refusal-loss": "n/a(no-net-debt-reduction)",
        "docs-refs": "n/a(no-doc-touched)",
        "docs-okf": "n/a(no-doc-touched)",
    }
    if "python" in _kinds(change):
        unread["closure"] = "n/a(the-tree-was-green)"
    return unread


# meiva:note
# Run what the matched triggers ask for and judge each run by its own stated verdict: an exit code,
# except pyright, whose red tree is attributed to this change rather than counted against it.
def _run_triggers(
    change: ChangeSet,
    matched: Sequence[Trigger],
    execute: Callable[[Sequence[str], Path], tuple[int, str, float]],
) -> tuple[dict[str, str], bool]:
    values: dict[str, str] = {}
    failed = False
    for trigger in matched:
        for run in trigger.runs(change):
            code, output, seconds = execute(
                run.argv, change.root / run.cwd if run.cwd else change.root
            )
            fields = _fields_for(run, code, output, seconds, change)
            attributed = fields.pop("_over_baseline", "")
            values.update(fields)
            verdict = attributed == "1" if run.label == "pyright" else code != 0
            failed = failed or (trigger.blocking and verdict)
    return values, failed


def check(
    change: ChangeSet,
    *,
    skip_tests: bool,
    execute: Callable[[Sequence[str], Path], tuple[int, str, float]],
    choose: Callable[[ChangeSet], Selection] = select_tests,
) -> tuple[dict[str, str], bool]:
    matched = [trigger for trigger in TRIGGERS if trigger.matches(change)]
    values: dict[str, str] = {"scope": _scope(change)}
    failed = False
    selection = choose(change)
    values["selector"] = selection.field
    values["blind"] = (
        f"BLIND({','.join(selection.blind)})"
        if selection.blind
        else "n/a(no-changed-file-was-invisible)"
    )
    values["fallback"] = (
        "n/a(a-blind-selection-has-no-fallback-yet)"
        if selection.blind
        else "n/a(the-selection-was-not-blind)"
    )
    ran, failed = _run_triggers(change, matched, execute)
    values.update(ran)
    for label, reason in _not_run(change).items():
        values.setdefault(label, reason)
    if skip_tests or not selection.tests:
        values["tests"] = f"skipped({'--skip-tests' if skip_tests else 'selector-chose-none'})"
        values["passed"] = "n/a(no-test-ran)"
        values["failed"] = "n/a(no-test-ran)"
        values["errors"] = "n/a(no-test-ran)"
        return values, failed
    code, output, seconds = execute(
        (sys.executable, "-m", "pytest", "-q", *selection.tests), change.root
    )
    values["tests"] = f"RAN({len(selection.tests)}files)"
    values["passed"] = f"{_count(output, 'passed')}tests({_spell(seconds)})"
    values["failed"] = f"{_count(output, 'failed')}tests"
    values["errors"] = f"{_error_count(output)}tests"
    return values, failed or code != 0


def build_receipt(values: dict[str, str], sha: str, base: str) -> str:
    fields = [f"{label}={values.get(label, 'n/a(unfilled)')}" for label in FIELD_ORDER]
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
    lines = [f"change: {len(change.files)} files against {change.base} scope={_scope(change)}"]
    for trigger in matched:
        lines.append(f"trigger {trigger.name} (blocking={trigger.blocking})")
        for run in trigger.runs(change):
            where = f" (in {run.cwd})" if run.cwd else ""
            lines.append(f"  {run.label}: {' '.join(run.argv[:4])}{where} [{run.judge}]")
    lines.append("  selector: scripts/ci/test_selector.py --json (the files it chooses)")
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
    judged = partial(run_child, environment=child_environment(os.environ))
    values, failed = check(change, skip_tests=arguments.skip_tests, execute=judged)
    print(build_receipt(values, sha, _git(root, "rev-parse", arguments.base)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
