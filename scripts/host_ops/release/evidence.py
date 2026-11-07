from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.options import options
from base.host.boundary.untyped import object_of
from scripts.host_ops.release.git_tags import git_run, is_ancestor, parse_utc_timestamp

WORKFLOW = ".github/workflows/release-gate.yml"
_MAIN = "refs/remotes/origin/main"
_MAX_AGE = timedelta(hours=24)
_SHA = re.compile(r"[0-9a-f]{40}")


def _object(value: object) -> Mapping[str, object]:
    return value if is_object_dict(value) else {}


def _array(value: object) -> Sequence[object]:
    return value if is_object_list(value) else ()


def _git_object(repository: Path, revision: str) -> str | None:
    result = git_run(repository, "rev-parse", "--verify", "--end-of-options", revision)
    return result.stdout.strip() if result.returncode == 0 else None


def _source_commit(run: Mapping[str, object], repo: str) -> str | None:
    sha = run.get("head_sha")
    if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
        return None
    if (
        run.get("path") not in (WORKFLOW, f"{WORKFLOW}@main", f"{WORKFLOW}@refs/heads/main")
        or run.get("display_title") != f"Verify release tree {sha}"
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "main"
        or _object(run.get("repository")).get("full_name") != repo
        or _object(run.get("head_repository")).get("full_name") != repo
    ):
        return None
    return sha


def _completed_run(run: Mapping[str, object], now: datetime) -> int | None:
    run_id, started = run.get("id"), run.get("run_started_at")
    if (
        type(run_id) is not int
        or run_id < 1
        or not isinstance(started, str)
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or type(run.get("run_attempt")) is not int
        or run.get("run_attempt") != 1
    ):
        return None
    try:
        age = now - parse_utc_timestamp(started, "run_started_at")[0]
    except ValueError:
        return None
    if not timedelta(0) <= age <= _MAX_AGE:
        return None
    return run_id


# meiva:note
# The API supplies provenance. Git binds the workflow to its reviewed definition,
# and the whole source tree binds locks, local actions, test configuration and test scope.
# Both commits must be on main: the release's migration lint compares against its main merge base.
def verified_run(
    repository: Path, repo: str, target: str, now: datetime, response: object
) -> int | None:
    if _SHA.fullmatch(target) is None or not is_ancestor(repository, target, _MAIN):
        return None
    tree = _git_object(repository, f"{target}^{{tree}}")
    if tree is None:
        return None
    expected = _git_object(repository, f"HEAD:{WORKFLOW}")
    if expected is None or _git_object(repository, f"{target}:{WORKFLOW}") != expected:
        return None
    for raw in _array(_object(response).get("workflow_runs")):
        run = _object(raw)
        sha = _source_commit(run, repo)
        if (
            sha is not None
            and _git_object(repository, f"{sha}^{{tree}}") == tree
            and is_ancestor(repository, sha, _MAIN)
        ):
            return _completed_run(run, now)
    return None


@dataclass(frozen=True)
class EvidenceOptions:
    repo: Path
    github_repository: str
    target: str
    now: str


def main(argv: Sequence[str], run_listing: TextIO) -> int:
    parser = argparse.ArgumentParser(description="Select a trusted full release verification run.")
    _ = parser.add_argument("--repo", type=Path, required=True)
    _ = parser.add_argument("--github-repository", required=True)
    _ = parser.add_argument("--target", required=True)
    _ = parser.add_argument("--now", required=True)
    args = options(parser.parse_args(argv), EvidenceOptions)
    try:
        response = object_of(json.load, run_listing)
    except json.JSONDecodeError:
        print("No usable verification run listing.", file=sys.stderr)
        return 0
    run_id = verified_run(
        args.repo,
        args.github_repository,
        args.target,
        parse_utc_timestamp(args.now, "--now")[0],
        response,
    )
    if run_id is not None:
        print(run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin))
