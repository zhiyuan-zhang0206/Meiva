# meiva:note
# The Trunk merge queue was retired on 2026-11-01 (PR #8081,
# `docs/decisions/engineering/tooling/2026-11-01-release-train-and-channels.md`):
# pull requests merge directly once their own checks are green, and
# `release-gate.yml` re-runs the full suite on `main` every six hours with
# `scripts/host_ops/release/bisect_train.py` localizing a cross-PR break. Queue
# submission, eviction, queue status and the merge-watch flow were deleted with it
# rather than left as refusals: an error message is still a promise that the path
# exists, and this one does not.

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping

from base.clock import Clock
from base.host.net.ports import UrlOpener
from base.host.system.hostport import Host
from scripts.ci.pull_requests.job_rerun import CiJobRerunError, list_failed_jobs, rerun_failed_jobs


def _rerun_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    host: Host,
) -> int | None:
    """Dispatch --rerun-failed-jobs when set; None when not set."""
    if not args.rerun_failed_jobs:
        return None
    if args.wait or args.json:
        parser.error("--rerun-failed-jobs is exclusive with --wait/--json")
    try:
        if args.dry_run:
            jobs = list_failed_jobs(args.pr, args.repo, host=host)
            if not jobs:
                print("No failed jobs to re-run")
                return 0
            for j in jobs:
                print(
                    f"{j['name']} (job {j['job_id']}, run {j['run_id']}, "
                    f"{j['conclusion']}) [run {j['run_status']}]"
                )
            return 0
        reran, waiting, errors = rerun_failed_jobs(args.pr, args.repo, host=host)
    except CiJobRerunError as error:
        # A failed GitHub query must not read as "no failed jobs" (issue #1945).
        print(f"Failed to list failed jobs: {error}", file=sys.stderr, flush=True)
        return 1
    for j in reran:
        print(f"Re-ran {j['name']} (job {j['job_id']})")
    for w in waiting:
        print(
            f"Waiting: {w['name']} (job {w['job_id']}, run {w['run_id']}): the run "
            f"is still {w['run_status']}; GitHub refuses re-runs until it completes. "
            "Re-run this command once the run finishes."
        )
    for e in errors:
        print(f"Re-run failed: {e}")
    return 3 if errors else 5 if waiting else 0


def dispatch(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    host: Host,
    clock: Clock,
    open_url: UrlOpener,
    workflows: bool,
    env: Mapping[str, str],
) -> int:
    _ = (clock, open_url, workflows, env)
    if args.pr is None:
        parser.error("PR number is required")
    rerun_rc = _rerun_command(args, parser, host=host)
    if rerun_rc is None:
        raise AssertionError("selected re-run command returned no result")
    return rerun_rc
