# meiva:link scripts/ci/pull_requests/docs/event-export.meiva.okf.md

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from base.clock import Timers
    from scripts.ci.pull_requests.export_process import GhProcess

__all__ = [
    "CiRunsError",
    "FetchStats",
    "fetch_closed_prs",
    "fetch_runs",
    "parse_timestamp",
    "run_gh",
]

_GH_TIMEOUT_SECONDS = 60.0
_PAGE_SIZE = 100
_MAX_GH_ATTEMPTS = 4


class CiRunsError(RuntimeError):
    # meiva:note
    # An authoritative GitHub response made this sampling pass unusable.
    pass


@dataclass
class FetchStats:
    # meiva:note
    # The per-sampler GitHub request accounting carried by the breadcrumb.

    api_requests: int = 0
    retries: int = 0


def parse_timestamp(value: object) -> datetime | None:
    # meiva:note
    # Parse a GitHub ISO timestamp as UTC; malformed optional values are absent.
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


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


def _json_list(output: str, description: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CiRunsError(f"invalid JSON from {description}") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise CiRunsError(f"unexpected {description} payload")
    return parsed


def fetch_runs(
    repo: str,
    since: datetime,
    until: datetime,
    stats: FetchStats,
    *,
    run_process: GhProcess,
    timers: Timers,
    max_pages: int,
) -> list[dict[str, Any]]:
    # meiva:note
    # Walk runs newest-first using created<= cursors and id deduplication.
    cursor = until
    seen: dict[int, dict[str, Any]] = {}
    jq = (
        "[.workflow_runs[] | {id, workflow_id, name, event, status, conclusion, run_attempt, "
        "head_branch, head_sha, created_at, run_started_at, updated_at}] | @json"
    )
    while cursor >= since:
        oldest: datetime | None = None
        for page in range(1, max_pages + 1):
            stamp = cursor.strftime("%Y-%m-%dT%H:%M:%SZ")
            path = (
                f"repos/{repo}/actions/runs?created=%3C%3D{stamp}&per_page={_PAGE_SIZE}&page={page}"
            )
            batch = _json_list(
                run_gh([path, "--jq", jq], stats, run_process=run_process, timers=timers),
                "workflow runs",
            )
            if not batch:
                return list(seen.values())
            for run in batch:
                identifier = run.get("id")
                created = parse_timestamp(run.get("created_at"))
                if not isinstance(identifier, int) or created is None:
                    raise CiRunsError("workflow run lacks an integer id or created_at")
                seen[identifier] = run
                oldest = created if oldest is None or created < oldest else oldest
            if len(batch) < _PAGE_SIZE:
                return list(seen.values())
        if oldest is None or oldest < since:
            return list(seen.values())
        next_cursor = oldest - timedelta(seconds=1)
        if next_cursor >= cursor:
            raise CiRunsError("workflow-runs cursor did not move backward")
        cursor = next_cursor
    return list(seen.values())


def fetch_closed_prs(
    repo: str,
    since: datetime,
    stats: FetchStats,
    *,
    run_process: GhProcess,
    timers: Timers,
    max_pages: int,
) -> list[dict[str, Any]]:
    # meiva:note
    # Walk the authoritative closed-PR population newest-updated first.
    jq = (
        "[.[] | {number, head_ref: .head.ref, head_sha: .head.sha, created_at, updated_at, "
        "merged_at, closed_at}] | @json"
    )
    records: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        path = f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={_PAGE_SIZE}&page={page}"
        batch = _json_list(
            run_gh([path, "--jq", jq], stats, run_process=run_process, timers=timers),
            "closed pull requests",
        )
        records.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return records
        stamps = [parse_timestamp(entry.get("updated_at")) for entry in batch]
        oldest = min((stamp for stamp in stamps if stamp is not None), default=None)
        if oldest is not None and oldest < since:
            return records
    raise CiRunsError(f"closed-PR walk exceeded {max_pages} pages")
