# meiva:note
# Run the checks one change needs and print a single-line summary receipt. It belongs beside the
# pre-push hooks that approximate it today: `scripts/` is at its entry cap and `scripts/ci/` may
# not reach `scripts/audit` without closing a cycle.

from __future__ import annotations

import argparse
import json
import os
import signal
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

from base.host.boundary.options import options  # noqa: E402
from base.host.boundary.untyped import object_of  # noqa: E402
from scripts.hooks.diff_checks_cache import (  # noqa: E402
    CACHE_FILENAME,
    CacheContext,
    CheckMode,
    cache_key,
    full_meizel_run,
    input_digest_for_run,
    is_cacheable_run,
    is_object_list,
    is_str_object_dict,
    lookup_run,
    lookup_selection,
    meizel_full_report,
    meizel_pass,
    meizel_run_key,
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
    receipt_verdict,
    reverse_closure,
    scope_of,
)
from scripts.hooks.diff_checks_literals import (  # noqa: E402
    MAX_LITERAL_AUTO_TESTS,
    LiteralScan,
    candidate_test_files,
    extract_deleted_literals,
    find_literal_test_hits,
    format_literal_tests_field,
    format_over_cap_notice,
    is_production_literal_source,
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
    pytest_command,
    pytest_counts,
    pytest_failure_evidence,
)
from scripts.hooks.diff_checks_pytest_child import (  # noqa: E402
    PytestInterrupted,
    run_pytest_child,
)
from scripts.hooks.diff_checks_scratch import (  # noqa: E402
    pytest_scratch,
    pytest_scratch_parent,
    retain_for,
)
from scripts.hooks.meizel_run import (  # noqa: E402
    cached_full_meizel,
    child_environment,
    input_fingerprint,
)

__all__ = [
    "FIELD_ORDER",
    "MAX_LITERAL_AUTO_TESTS",
    "PYRIGHT_ERROR_BASELINE",
    "RECEIPT_VERSION",
    "ChangeSet",
    "CheckMode",
    "LiteralScan",
    "PytestCounts",
    "Run",
    "Selection",
    "build_receipt",
    "changed_files",
    "check",
    "child_environment",
    "diff_text_for",
    "extract_deleted_literals",
    "find_literal_test_hits",
    "main",
    "open_cache_context",
    "parse_args",
    "plan",
    "pytest_counts",
    "receipt_verdict",
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


def diff_text_for(change: ChangeSet) -> str:
    if not change.files or not change.root.is_dir():
        return ""
    sources = tuple(path for path in change.files if is_production_literal_source(path))
    if not sources:
        return ""
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "-U0",
                "--no-color",
                "--no-ext-diff",
                f"{change.base}...HEAD",
                "--",
                *sources,
            ],
            cwd=change.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return ""
    return result.stdout if result.returncode == 0 else ""


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
    payload = object_of(json.loads, result.stdout)
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


def open_cache_context(
    root: Path, base: str, *, python: str = "", environment: Mapping[str, str] | None = None
) -> CacheContext | None:
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
        head = _git(root, "rev-parse", "HEAD")
        head_tree = _git(root, "rev-parse", f"{head}^{{tree}}")
        base_sha = _git(root, "rev-parse", f"{base}^{{commit}}")
        merge_base_sha = _git(root, "merge-base", base_sha, head)
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
        merge_base_sha=merge_base_sha,
        meizel_input=(
            partial(input_fingerprint, root, head, python=python, environment=environment)
            if environment is not None
            else None
        ),
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


def _run_cache_key(
    cache: CacheContext, run: Run, change: ChangeSet, *, mode: CheckMode
) -> str | None:
    if full_meizel_run(run.label, run.argv) or run.label == "debt":
        return meizel_run_key(cache, run.label, run.argv)
    return cache_key(
        mode=mode,
        label=run.label,
        argv=run.argv,
        cwd=run.cwd,
        input_state_digest=input_digest_for_run(cache, change.root, run.label, run.argv),
    )


def _cached_fields(cache: CacheContext, key: str, *, full_meizel: bool) -> dict[str, str] | None:
    if full_meizel:
        return cached_full_meizel(cache, key)
    cached = lookup_run(cache, key)
    return cached.fields if cached is not None and cached.code == 0 else None


def _record_checked_run(
    cache: CacheContext, key: str, run: Run, change: ChangeSet, *, code: int, output: str
) -> None:
    cached_fields = fields_for(run, code, output, 0.0, change)
    full_meizel = full_meizel_run(run.label, run.argv)
    if full_meizel and not (meizel_full_report(output) and meizel_pass(cached_fields)):
        return
    if (full_meizel or run.label == "debt") and meizel_run_key(cache, run.label, run.argv) != key:
        return
    record_run(cache, key, code=code, output=output, fields=cached_fields)


def _execute_run(
    run: Run,
    change: ChangeSet,
    *,
    execute: Callable[[Sequence[str], Path], tuple[int, str, float]],
    mode: CheckMode,
    cache: CacheContext | None,
) -> dict[str, str]:
    full_meizel = full_meizel_run(run.label, run.argv)
    key = _run_cache_key(cache, run, change, mode=mode) if cache is not None else None
    if (
        cache is not None
        and key is not None
        and (proof := _cached_fields(cache, key, full_meizel=full_meizel)) is not None
    ):
        return dict(proof)
    code, output, seconds = execute(run.argv, change.root / run.cwd if run.cwd else change.root)
    fields = fields_for(run, code, output, seconds, change)
    if cache is not None and key is not None and is_cacheable_run(code, fields):
        _record_checked_run(cache, key, run, change, code=code, output=output)
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
    python: str,
) -> tuple[PytestCounts, bool]:
    logical_argv = pytest_command(tests, python=python)
    key = ""
    if cache is not None:
        key = cache_key(
            mode=mode,
            label="pytest",
            argv=logical_argv,
            cwd="",
            input_state_digest=input_digest_for_run(cache, change.root, "pytest", logical_argv),
        )
        cached = lookup_run(cache, key)
        if cached is not None and cached.fields is not None:
            return PytestCounts(dict(cached.fields), broken=False), False
    parent = pytest_scratch_parent(os.environ)
    with pytest_scratch(parent) as report_dir:
        report = report_dir / "pytest-report.xml"
        try:
            code, output, seconds = execute(pytest_argv(report, tests, python=python), change.root)
        except BaseException as error:
            if not retain_for(error):
                pytest_failure_evidence(report, f"pytest interrupted: {type(error).__name__}")
            raise
        counts = pytest_counts(report, selected_files=len(tests), seconds=seconds)
        if (
            cache is not None
            and code == 0
            and not counts.broken
            and counts.values.get("failed") == "0tests"
            and counts.values.get("errors") == "0tests"
        ):
            cached_counts = pytest_counts(report, selected_files=len(tests), seconds=0.0)
            record_run(
                cache,
                key,
                code=0,
                output=report.read_text(encoding="utf-8"),
                fields=cached_counts.values,
            )
        if code != 0 or counts.broken:
            pytest_failure_evidence(report, output)
        return counts, code != 0 or counts.broken


def _run_literal_tests(
    change: ChangeSet,
    selection: Selection,
    *,
    skip_literal_tests: bool,
    read_diff: Callable[[ChangeSet], str],
    execute: Callable[[Sequence[str], Path], tuple[int, str, float]],
    mode: CheckMode,
    cache: CacheContext | None,
    python: str,
) -> tuple[str, bool]:
    if skip_literal_tests:
        return "skipped(--skip-literal-tests)", False
    literals = extract_deleted_literals(read_diff(change))
    if not literals:
        return "0hits", False
    candidates = candidate_test_files(
        change.root,
        change.files,
        selection.tests,
        blind=selection.blind,
        decision=selection.decision,
    )
    scan = find_literal_test_hits(change.root, literals, candidates)
    if not scan.hits:
        return "0hits", False
    if len(scan.hits) > MAX_LITERAL_AUTO_TESTS:
        print(format_over_cap_notice(scan.hits), file=sys.stderr)
        return f"over-cap({len(scan.hits)}files>{MAX_LITERAL_AUTO_TESTS})", False
    print(
        f"diff-checks literal-tests: auto-running {len(scan.hits)} hit file(s): "
        f"{', '.join(scan.hits)}",
        file=sys.stderr,
    )
    counts, pytest_failed = _run_pytest(
        change,
        scan.hits,
        execute=execute,
        mode=mode,
        cache=cache,
        python=python,
    )
    return (
        format_literal_tests_field(len(scan.hits), counts, pytest_failed=pytest_failed),
        pytest_failed,
    )


def check(
    change: ChangeSet,
    *,
    skip_tests: bool,
    execute: Callable[[Sequence[str], Path], tuple[int, str, float]],
    python: str,
    choose: Callable[[ChangeSet], Selection] = select_tests,
    mode: CheckMode = "incremental",
    cache: CacheContext | None = None,
    execute_pytest: Callable[[Sequence[str], Path], tuple[int, str, float]] | None = None,
    skip_literal_tests: bool = False,
    read_diff: Callable[[ChangeSet], str] | None = None,
) -> tuple[dict[str, str], bool]:
    groups = schedule_groups(change, mode=mode, cache=cache, python=python)
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
        if skip_tests:
            literal_field, literal_failed = _run_literal_tests(
                change,
                selection,
                skip_literal_tests=skip_literal_tests,
                read_diff=read_diff or diff_text_for,
                execute=execute_pytest or execute,
                mode=mode,
                cache=cache,
                python=python,
            )
            values["literal-tests"] = literal_field
            return values, failed or literal_failed
        values["literal-tests"] = "n/a(selector-chose-none)"
        return values, failed
    counts, pytest_failed = _run_pytest(
        change,
        selection.tests,
        execute=execute_pytest or execute,
        mode=mode,
        cache=cache,
        python=python,
    )
    values.update(counts.values)
    values["literal-tests"] = "n/a(covered-by-tests)"
    return values, failed or pytest_failed


def plan(
    change: ChangeSet,
    *,
    skip_tests: bool,
    python: str,
    mode: CheckMode = "incremental",
    skip_literal_tests: bool = False,
) -> list[str]:
    groups = schedule_groups(change, mode=mode, python=python)
    return render_plan(
        change,
        groups,
        skip_tests=skip_tests,
        mode=mode,
        skip_literal_tests=skip_literal_tests,
    )


# meiva:note
# The flags `parse_args` declares, read once so `main` holds typed values instead of the parser's `Any`.
@dataclass(frozen=True)
class Options:
    base: str
    mode: CheckMode
    no_cache: bool
    changed_files: Path | None
    skip_tests: bool
    skip_literal_tests: bool
    dry_run: bool
    repo_root: Path


def parse_args(argv: Sequence[str]) -> Options:
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
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help=(
            "select tests, skip the full test run (still auto-runs deleted-literal hits "
            f"up to {MAX_LITERAL_AUTO_TESTS} files unless --skip-literal-tests is set)"
        ),
    )
    parser.add_argument(
        "--skip-literal-tests",
        action="store_true",
        help="skip the deleted-literal test scan and auto-run under --skip-tests",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and run nothing")
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    return options(parser.parse_args(argv), Options)


def main(argv: Sequence[str]) -> int:
    arguments = parse_args(argv)
    # meiva:note
    # This process's interpreter, read once for the commands that do not name a first-party entry:
    # the checks, the tools beside it, pytest. The selector keeps its own read in `select_tests`,
    # because that command names `scripts.ci.test_selector` and the read is what puts it in the
    # graph.
    python = sys.executable
    mode = arguments.mode
    root = arguments.repo_root.resolve()
    base_sha = _git(
        root, "rev-parse", "--verify", "--end-of-options", f"{arguments.base}^{{commit}}"
    )
    files = (
        tuple(
            line.strip()
            for line in arguments.changed_files.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if arguments.changed_files is not None
        else changed_files(base_sha, root)
    )
    change = ChangeSet(root=root, base=base_sha, files=files)
    sha = _git(root, "rev-parse", "HEAD")
    if mode == "incremental" and not change.files:
        print(f"{RECEIPT_VERSION} @{sha} no-changes")
        return 0
    if arguments.dry_run:
        print(
            "\n".join(
                plan(
                    change,
                    skip_tests=arguments.skip_tests,
                    mode=mode,
                    python=python,
                    skip_literal_tests=arguments.skip_literal_tests,
                )
            )
        )
        return 0
    environment = child_environment(os.environ)
    cache = (
        None
        if arguments.no_cache or arguments.changed_files is not None
        else open_cache_context(
            root,
            base_sha,
            python=python,
            environment=environment if root == REPOSITORY_ROOT else None,
        )
    )
    judged = partial(run_child, environment=environment)
    values, failed = check(
        change,
        skip_tests=arguments.skip_tests,
        skip_literal_tests=arguments.skip_literal_tests,
        execute=judged,
        execute_pytest=partial(run_pytest_child, environment=environment),
        mode=mode,
        cache=cache,
        python=python,
    )
    print(build_receipt(values, sha, base_sha, failed=failed))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except PytestInterrupted as interruption:
        signal.raise_signal(interruption.signum)
        raise SystemExit(128 + interruption.signum) from None
