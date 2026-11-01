# meiva:note
# Run the checks one change needs and print a single-line summary receipt. It belongs beside the
# pre-push hooks that approximate it today: `scripts/` is at its entry cap and `scripts/ci/` may
# not reach `scripts/audit` without closing a cycle.

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import assert_never

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# meiva:note
# A standalone script runs with its own directory off `sys.path`, so the repository root is put
# back before the sibling modules this file imports: plan construction, receipt field parsing,
# pytest JUnit XML counting and sub-check verdict caching live beside execution so each stays well
# within the file budget.
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.hooks.diff_checks_cache import (  # noqa: E402
    CACHE_FILENAME,
    CacheContext,
    CheckMode,
    cache_key,
    input_digest_for_run,
    is_cacheable_run,
    is_object_list,
    is_str_object_dict,
    lookup_run,
    lookup_selection,
    python_tree_digest,
    record_run,
    record_selection,
)
from scripts.hooks.diff_checks_fields import (  # noqa: E402
    FIELD_ORDER,
    RECEIPT_VERSION,
    ChangeSet,
    Run,
    build_receipt,
    fields_for,
    not_run_fields,
    reverse_closure,
    scope_of,
)
from scripts.hooks.diff_checks_plan import (  # noqa: E402
    PYRIGHT_ERROR_BASELINE,
    ScheduledGroup,
    render_plan,
    schedule_groups,
)
from scripts.hooks.diff_checks_pytest import (  # noqa: E402
    PytestCounts,
    pytest_argv,
    pytest_counts,
)

__all__ = [
    "FIELD_ORDER",
    "PYRIGHT_ERROR_BASELINE",
    "RECEIPT_VERSION",
    "ChangeSet",
    "CheckMode",
    "PytestCounts",
    "Run",
    "Selection",
    "build_receipt",
    "changed_files",
    "check",
    "child_environment",
    "main",
    "open_cache_context",
    "parse_args",
    "plan",
    "pytest_counts",
    "reverse_closure",
    "run_child",
    "select_tests",
]


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def changed_files(base: str, root: Path) -> tuple[str, ...]:
    listing = _git(root, "diff", "--name-only", f"{base}...HEAD")
    return tuple(line.strip() for line in listing.splitlines() if line.strip())


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
    payload: object = json.loads(result.stdout)
    if not is_str_object_dict(payload):
        return Selection(decision="UNKNOWN", tests=(), blind=())
    raw_tests = payload.get("tests")
    raw_blind = payload.get("blind_changed")
    tests = tuple(str(item) for item in raw_tests) if is_object_list(raw_tests) else ()
    blind = tuple(str(item) for item in raw_blind) if is_object_list(raw_blind) else ()
    return Selection(
        decision=str(payload.get("decision", "UNKNOWN")),
        tests=tests,
        blind=blind,
    )


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


def open_cache_context(root: Path, base: str) -> CacheContext | None:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        if status.stdout != "":
            return None
        common_dir = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        head_tree = _git(root, "rev-parse", "HEAD^{tree}")
        base_sha = _git(root, "rev-parse", f"{base}^{{commit}}")
        ls_tree = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    return CacheContext(
        cache_path=common_dir / CACHE_FILENAME,
        head_tree=head_tree,
        base_sha=base_sha,
        python_digest=python_tree_digest(ls_tree),
    )


def _choose_selection(
    change: ChangeSet,
    *,
    choose: Callable[[ChangeSet], Selection],
    mode: CheckMode,
    cache: CacheContext | None,
) -> Selection:
    if cache is None:
        return choose(change)
    argv = ("test_selector", *change.files)
    key = cache_key(
        mode=mode,
        label="selector",
        argv=argv,
        cwd="",
        input_state_digest=input_digest_for_run(cache, change.root, "selector", argv),
    )
    cached = lookup_selection(cache, key)
    if cached is not None:
        return Selection(decision=cached.decision, tests=cached.tests, blind=cached.blind)
    selection = choose(change)
    if selection.decision != "UNKNOWN":
        record_selection(
            cache,
            key,
            decision=selection.decision,
            tests=selection.tests,
            blind=selection.blind,
        )
    return selection


def _execute_run(
    run: Run,
    change: ChangeSet,
    *,
    execute: Callable[[Sequence[str], Path], tuple[int, str, float]],
    mode: CheckMode,
    cache: CacheContext | None,
) -> dict[str, str]:
    key = ""
    if cache is not None:
        key = cache_key(
            mode=mode,
            label=run.label,
            argv=run.argv,
            cwd=run.cwd,
            input_state_digest=input_digest_for_run(cache, change.root, run.label, run.argv),
        )
        cached = lookup_run(cache, key)
        if cached is not None:
            return fields_for(run, cached.code, cached.output, 0.0, change)
    code, output, seconds = execute(run.argv, change.root / run.cwd if run.cwd else change.root)
    fields = fields_for(run, code, output, seconds, change)
    if cache is not None and is_cacheable_run(code, fields):
        record_run(cache, key, code=code, output=output)
    if code != 0:
        fields["_exit_nonzero"] = "1"
    return fields


def _verdict_for_run(mode: CheckMode, label: str, *, nonzero: bool, attributed: str) -> bool:
    if mode == "incremental":
        return attributed == "1" if label == "pyright" else nonzero
    if mode == "full":
        return nonzero or attributed == "1"
    assert_never(mode)


# meiva:note
# Run what the scheduled groups ask for and judge each run by its own stated verdict: an exit code,
# except incremental pyright, whose red tree is attributed to this change rather than counted
# against it; full mode judges whole-tree exit status directly.
def _run_groups(
    change: ChangeSet,
    groups: Sequence[ScheduledGroup],
    execute: Callable[[Sequence[str], Path], tuple[int, str, float]],
    *,
    mode: CheckMode,
    cache: CacheContext | None,
) -> tuple[dict[str, str], bool]:
    values: dict[str, str] = {}
    failed = False
    for group in groups:
        for run in group.runs:
            fields = _execute_run(run, change, execute=execute, mode=mode, cache=cache)
            attributed = fields.pop("_over_baseline", "")
            nonzero = fields.pop("_exit_nonzero", "") == "1"
            values.update(fields)
            verdict = _verdict_for_run(mode, run.label, nonzero=nonzero, attributed=attributed)
            failed = failed or (group.blocking and verdict)
    return values, failed


def _run_pytest(
    change: ChangeSet,
    tests: tuple[str, ...],
    *,
    execute: Callable[[Sequence[str], Path], tuple[int, str, float]],
    mode: CheckMode,
    cache: CacheContext | None,
) -> tuple[PytestCounts, bool]:
    logical_argv = (sys.executable, "-m", "pytest", "-q", *tests)
    key = ""
    report_dir = Path(tempfile.mkdtemp(prefix="diff-checks-"))
    report = report_dir / "pytest-report.xml"
    try:
        if cache is not None:
            key = cache_key(
                mode=mode,
                label="pytest",
                argv=logical_argv,
                cwd="",
                input_state_digest=input_digest_for_run(cache, change.root, "pytest", logical_argv),
            )
            cached = lookup_run(cache, key)
            if cached is not None:
                report.write_text(cached.output, encoding="utf-8")
                counts = pytest_counts(report, selected_files=len(tests), seconds=0.0)
                return counts, counts.broken
        code, _output, seconds = execute(pytest_argv(report, tests), change.root)
        counts = pytest_counts(report, selected_files=len(tests), seconds=seconds)
        if (
            cache is not None
            and code == 0
            and not counts.broken
            and counts.values.get("failed") == "0tests"
            and counts.values.get("errors") == "0tests"
        ):
            record_run(cache, key, code=0, output=report.read_text(encoding="utf-8"))
        return counts, code != 0 or counts.broken
    finally:
        shutil.rmtree(report_dir, ignore_errors=True)


def check(
    change: ChangeSet,
    *,
    skip_tests: bool,
    execute: Callable[[Sequence[str], Path], tuple[int, str, float]],
    choose: Callable[[ChangeSet], Selection] = select_tests,
    mode: CheckMode = "incremental",
    cache: CacheContext | None = None,
) -> tuple[dict[str, str], bool]:
    groups = schedule_groups(
        change, mode=mode, debt_reduced=_net_debt_reduction(change), cache=cache
    )
    values: dict[str, str] = {"scope": scope_of(change)}
    selection = _choose_selection(change, choose=choose, mode=mode, cache=cache)
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
    ran, failed = _run_groups(change, groups, execute, mode=mode, cache=cache)
    values.update(ran)
    for label, reason in not_run_fields(change, pyright_ran="pyright" in ran).items():
        values.setdefault(label, reason)
    if skip_tests or not selection.tests:
        values["tests"] = f"skipped({'--skip-tests' if skip_tests else 'selector-chose-none'})"
        values["passed"] = "n/a(no-test-ran)"
        values["failed"] = "n/a(no-test-ran)"
        values["errors"] = "n/a(no-test-ran)"
        return values, failed
    counts, pytest_failed = _run_pytest(
        change, selection.tests, execute=execute, mode=mode, cache=cache
    )
    values.update(counts.values)
    return values, failed or pytest_failed


def plan(
    change: ChangeSet,
    *,
    skip_tests: bool,
    mode: CheckMode = "incremental",
) -> list[str]:
    groups = schedule_groups(change, mode=mode, debt_reduced=_net_debt_reduction(change))
    return render_plan(change, groups, skip_tests=skip_tests, mode=mode)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="the commit to compare HEAD with")
    parser.add_argument(
        "--mode",
        choices=("incremental", "full"),
        default="incremental",
        help="incremental checks only changed scopes/closures (default); full sweeps the whole tree",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass reading and writing the sub-check verdict cache",
    )
    parser.add_argument(
        "--changed-files",
        type=Path,
        help="a newline-separated list to use instead of the git diff (re-running a past change)",
    )
    parser.add_argument("--skip-tests", action="store_true", help="select tests, run none of them")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and run nothing")
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    return parser.parse_args(argv)


def _validated_mode(raw: object) -> CheckMode:
    if raw == "incremental":
        return "incremental"
    if raw == "full":
        return "full"
    raise ValueError(f"unsupported diff_checks mode: {raw!r}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    mode = _validated_mode(arguments.mode)
    root = arguments.repo_root.resolve()
    files = (
        tuple(
            line.strip()
            for line in arguments.changed_files.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if arguments.changed_files is not None
        else changed_files(arguments.base, root)
    )
    change = ChangeSet(root=root, base=arguments.base, files=files)
    sha = _git(root, "rev-parse", "HEAD")
    if mode == "incremental" and not change.files:
        print(f"{RECEIPT_VERSION} @{sha} no-changes")
        return 0
    if arguments.dry_run:
        print("\n".join(plan(change, skip_tests=arguments.skip_tests, mode=mode)))
        return 0
    cache = (
        None
        if arguments.no_cache or arguments.changed_files is not None
        else open_cache_context(root, arguments.base)
    )
    judged = partial(run_child, environment=child_environment(os.environ))
    values, failed = check(
        change,
        skip_tests=arguments.skip_tests,
        execute=judged,
        mode=mode,
        cache=cache,
    )
    print(build_receipt(values, sha, _git(root, "rev-parse", arguments.base)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
