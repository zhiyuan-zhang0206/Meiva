# meiva:link docs/contributing.md

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

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from base.host.boundary.json_values import is_object_dict, is_object_list  # noqa: E402
from base.host.boundary.options import options  # noqa: E402
from base.host.boundary.untyped import object_of  # noqa: E402

DEFAULT_WORKERS = 2
MAX_WORKERS = 4
DEFAULT_MAX_FILES = 400
DEFAULT_TIMEOUT_SECONDS = 1800
SELECTOR_TIMEOUT_SECONDS = 600
TERM_WAIT_SECONDS = 15
KILL_WAIT_SECONDS = 5

EXIT_PASS = 0
EXIT_TEST_FAILURE = 1
EXIT_NOT_RUN = 2


@dataclass(frozen=True)
class Options:
    base: str
    repo_root: Path
    workers: int
    max_files: int
    timeout: int
    dry_run: bool


@dataclass(frozen=True)
class Selection:
    decision: str
    reason: str
    tests: tuple[str, ...]
    blind: tuple[str, ...]


Selector = Callable[[Path, Sequence[str], str], Selection]
Runner = Callable[[Path, Sequence[str], Mapping[str, str], int], int | None]


def parse_args(argv: Sequence[str]) -> Options:
    parser = argparse.ArgumentParser(
        description="Run the pytest files this branch's changes affect (see docs/contributing.md)."
    )
    parser.add_argument(
        "--base",
        default="",
        help="commit to measure the change from (default: merge-base with origin/main, as pre-push)",
    )
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"pytest-xdist workers, 1 to {MAX_WORKERS} (default {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help=f"refuse a selection larger than this many files (default {DEFAULT_MAX_FILES})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"seconds before the pytest run is killed (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the selection, run nothing")
    parsed = options(parser.parse_args(argv), Options)
    if not 1 <= parsed.workers <= MAX_WORKERS:
        parser.error(f"--workers must be between 1 and {MAX_WORKERS}")
    if parsed.max_files < 1 or parsed.timeout < 1:
        parser.error("--max-files and --timeout must be positive")
    return parsed


def child_environment(parent: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in parent.items() if not name.startswith("MEIVA_")}


def captured(argv: Sequence[str], root: Path, *, timeout: int = 60) -> bytes:
    ran = subprocess.run(  # noqa: S603
        list(argv), cwd=root, capture_output=True, check=False, timeout=timeout
    )
    if ran.returncode != 0:
        tail = ran.stderr.decode(errors="replace").strip()[-400:]
        raise RuntimeError(f"{' '.join(argv[:4])} failed with exit {ran.returncode}: {tail}")
    return ran.stdout


def merge_base(root: Path, base: str, environment: Mapping[str, str]) -> str:
    if base:
        resolved = captured(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"], root
        )
        return resolved.decode().strip()
    ran = subprocess.run(  # noqa: S603
        ["bash", str(root / "scripts/hooks/prepush-base.sh")],
        cwd=root,
        env=dict(environment),
        capture_output=True,
        text=True,
        check=False,
    )
    if ran.returncode != 0:
        raise RuntimeError(ran.stderr.strip() or "scripts/hooks/prepush-base.sh failed")
    return ran.stdout.strip()


def changed_files(root: Path, base_sha: str) -> tuple[str, ...]:
    tracked = captured(["git", "diff", "--name-only", "--no-renames", "-z", base_sha, "--"], root)
    untracked = captured(["git", "ls-files", "--others", "--exclude-standard", "-z"], root)
    names = {raw.decode() for raw in (*tracked.split(b"\0"), *untracked.split(b"\0")) if raw}
    return tuple(sorted(names))


def select_tests(root: Path, files: Sequence[str], base_sha: str, *, python: str) -> Selection:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as listing:
        listing.write("\n".join(files) + "\n")
        listing_path = Path(listing.name)
    try:
        ran = captured(
            [
                python,
                "-m",
                "scripts.ci.test_selector",
                "--changed-files",
                str(listing_path),
                "--base-ref",
                base_sha,
                "--repo-root",
                str(root),
                "--json",
            ],
            root,
            timeout=SELECTOR_TIMEOUT_SECONDS,
        )
    finally:
        listing_path.unlink(missing_ok=True)
    payload = object_of(json.loads, ran)
    if not is_object_dict(payload):
        raise RuntimeError("test selector printed something other than a JSON object")
    return Selection(
        decision=str(payload.get("decision", "UNKNOWN")),
        reason=str(payload.get("reason", "no reason given")),
        tests=strings(payload.get("tests")),
        blind=strings(payload.get("blind_changed")),
    )


def strings(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if is_object_list(value) else ()


def pytest_argv(tests: Sequence[str], *, python: str, workers: int) -> list[str]:
    return [
        python,
        "-m",
        "pytest",
        "-q",
        "-rfE",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        f"-n={workers}",
        "--dist=loadfile",
        "--",
        *tests,
    ]


def run_pytest(
    root: Path, argv: Sequence[str], environment: Mapping[str, str], timeout: int
) -> int | None:
    child = subprocess.Popen(  # noqa: S603
        list(argv), cwd=root, env=dict(environment), start_new_session=True
    )
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_group(child)
        return None
    except BaseException:
        stop_group(child)
        raise


def stop_group(child: subprocess.Popen[bytes]) -> None:
    for signum, grace in ((signal.SIGTERM, TERM_WAIT_SECONDS), (signal.SIGKILL, KILL_WAIT_SECONDS)):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            return
        try:
            child.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def verdict(line: str) -> str:
    return f"affected-tests: {line}"


def main(
    argv: Sequence[str],
    *,
    parent_environment: Mapping[str, str],
    python: str,
    selector: Selector,
    runner: Runner,
) -> int:
    arguments = parse_args(argv)
    environment = child_environment(parent_environment)
    root = arguments.repo_root
    base_sha = merge_base(root, arguments.base, parent_environment)
    files = changed_files(root, base_sha)
    if not files:
        print(verdict(f"PASS nothing differs from {base_sha[:12]}"))
        return EXIT_PASS
    selection = selector(root, files, base_sha)
    print(
        f"affected-tests: {len(files)} changed file(s) since {base_sha[:12]}; selector "
        f"{selection.decision} ({selection.reason}), {len(selection.tests)} test file(s)"
    )
    if selection.decision == "SKIP":
        print(verdict("PASS no test is affected"))
        return EXIT_PASS
    if selection.decision != "SELECTED":
        blind = f"; unmapped: {', '.join(selection.blind)}" if selection.blind else ""
        print(
            verdict(
                f"NOT RUN the selector cannot bound this change ({selection.reason}){blind}; CI runs the suite"
            ),
            file=sys.stderr,
        )
        return EXIT_NOT_RUN
    if len(selection.tests) > arguments.max_files:
        print(
            verdict(
                f"NOT RUN {len(selection.tests)} test files exceed --max-files {arguments.max_files}; "
                "raise it or leave this change to CI"
            ),
            file=sys.stderr,
        )
        return EXIT_NOT_RUN
    if arguments.dry_run:
        print("\n".join(selection.tests))
        return EXIT_PASS
    started = time.monotonic()
    code = runner(
        root,
        pytest_argv(selection.tests, python=python, workers=arguments.workers),
        environment,
        arguments.timeout,
    )
    seconds = round(time.monotonic() - started)
    if code is None:
        print(
            verdict(f"FAIL timed out after {arguments.timeout}s ({seconds}s run, killed)"),
            file=sys.stderr,
        )
        return EXIT_TEST_FAILURE
    if code == 0:
        print(verdict(f"PASS {len(selection.tests)} test file(s) in {seconds}s"))
        return EXIT_PASS
    print(
        verdict(
            f"FAIL pytest exit {code} after {seconds}s; the FAILED/ERROR lines above name the tests"
        ),
        file=sys.stderr,
    )
    return EXIT_TEST_FAILURE


if __name__ == "__main__":
    try:
        raise SystemExit(
            main(
                sys.argv[1:],
                parent_environment=os.environ,
                python=sys.executable,
                selector=partial(select_tests, python=sys.executable),
                runner=run_pytest,
            )
        )
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(verdict(f"NOT RUN {error}"), file=sys.stderr)
        raise SystemExit(EXIT_NOT_RUN) from None
