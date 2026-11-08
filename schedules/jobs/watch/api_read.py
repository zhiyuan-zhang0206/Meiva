# meiva:note
# One GitHub API read for the watch jobs, with the bounded retry that the CI scripts' transport
# already owns (`scripts/ci/pull_requests/runs_api.run_gh`): a transient answer is retried a fixed
# number of times with a growing wait, and only then does the read fail.
#
# What this adds is the identification the round line needs -- how many attempts the read took.
# `gh api` answering 502 says nothing about the run: on 2026-11-08 one run's `jobs` endpoint
# answered 502 on all three machines for two hours, and an empty answer would have read as "no
# failed jobs" (task #8677). The read is a GET: an unreadable one raises, and the caller names the
# stream it belonged to.
# meiva:link schedules/README.md

from __future__ import annotations

from base.clock import Timers
from scripts.ci.pull_requests import runs_api
from scripts.ci.pull_requests.gh_process import GhProcess

__all__ = ["read_api"]


def read_api(path: str, *, run_process: GhProcess, timers: Timers) -> str:
    # meiva:note
    # The attempt count comes from the transport's own accounting, read after the fact: `retries`
    # counts the waits it took between attempts, so a read that gave up spent one more attempt than
    # it slept -- and `api_requests` alone would undercount a read that timed out, which the
    # transport counts as retryable but not as a request.
    stats = runs_api.FetchStats()
    try:
        return runs_api.run_gh([path], stats, run_process=run_process, timers=timers)
    except runs_api.CiRunsError as exc:
        attempts = stats.retries + 1
        raise RuntimeError(f"{exc} (unreadable after {attempts} attempt(s))") from exc
