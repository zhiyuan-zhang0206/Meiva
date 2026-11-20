# meiva:link scripts/ci/pull_requests/docs/event-export.md

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from base.clock import Timers
from scripts.ci.pull_requests.gh_process import GhProcess

__all__ = [
    "CiRunsError",
    "FetchStats",
    "run_gh",
]

_GH_TIMEOUT_SECONDS = 60.0
_MAX_GH_ATTEMPTS = 4


class CiRunsError(RuntimeError):
    # meiva:note
    # An authoritative GitHub response made this read unusable.
    pass


@dataclass
class FetchStats:
    # meiva:note
    # The caller's GitHub request accounting: requests issued and transient retries.

    api_requests: int = 0
    retries: int = 0


def run_gh(args: list[str], stats: FetchStats, *, run_process: GhProcess, timers: Timers) -> str:
    # meiva:note
    # Issue one read-only gh command with bounded transient-error retries.
    for attempt in range(_MAX_GH_ATTEMPTS):
        try:
            result = run_process(
                ["gh", "api", "--method", "GET", *args],
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            detail = str(exc)
            retryable = True
        else:
            stats.api_requests += 1
            if result.returncode == 0:
                return result.stdout
            detail = (result.stderr or result.stdout or "").strip()
            retryable = any(code in detail for code in ("429", "500", "502", "503", "504"))
        if not retryable or attempt == _MAX_GH_ATTEMPTS - 1:
            raise CiRunsError(f"gh api {' '.join(args[:2])} failed: {detail[-500:]}")
        stats.retries += 1
        timers.sleep(2 * (attempt + 1))
    raise AssertionError("bounded gh retry loop exhausted without result")
