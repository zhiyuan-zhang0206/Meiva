# meiva:link scripts/ci/docs/file-shard-shadow/required-ci.meiva.okf.md

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PlanRun:
    repo: str
    run_id: int
    attempt: int
    floor: int
    source_sha: str
    producer: str


def records(pages: object, field: str) -> list[dict[str, Any]]:
    if not isinstance(pages, list) or not pages:
        raise ValueError("GitHub pagination must contain at least one page")
    result: list[dict[str, Any]] = []
    totals: set[int] = set()
    for page in pages:
        if not isinstance(page, dict) or type(page["total_count"]) is not int:
            raise ValueError("GitHub page requires an integer total_count")
        batch = page[field]
        if not isinstance(batch, list) or not all(isinstance(row, dict) for row in batch):
            raise ValueError(f"GitHub page requires a list of {field} objects")
        totals.add(page["total_count"])
        result.extend(batch)
    if totals != {len(result)}:
        raise ValueError("GitHub pagination did not deliver the complete population")
    _validate_record_ids(result)
    return result


def _validate_record_ids(rows: list[dict[str, Any]]) -> None:
    identifiers = [row["id"] for row in rows]
    if any(type(identifier) is not int or identifier <= 0 for identifier in identifiers):
        raise ValueError("GitHub records require positive native ids")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("GitHub pagination repeated a native record")


def _producer_versions(jobs: list[dict[str, Any]], run: PlanRun) -> dict[int, dict[str, Any]]:
    producers = [job for job in jobs if job["name"] == run.producer]
    attempts: dict[int, dict[str, Any]] = {}
    for job in producers:
        attempt = job["run_attempt"]
        if type(attempt) is not int or not 1 <= attempt <= run.attempt:
            raise ValueError("Producer has an invalid or future run_attempt")
        if (
            type(job["run_id"]) is not int
            or job["run_id"] != run.run_id
            or job["head_sha"] != run.source_sha
        ):
            raise ValueError("Producer belongs to another run or source")
        if attempt in attempts:
            raise ValueError("Producer identity is ambiguous within one attempt")
        attempts[attempt] = job
    return attempts


def _successful_generation(job: dict[str, Any], view: int) -> int:
    steps = job["steps"]
    if not isinstance(steps, list) or not all(
        isinstance(step, dict) and isinstance(step["name"], str) for step in steps
    ):
        raise ValueError("Successful producer requires native step objects")
    markers = [step for step in steps if step["name"].startswith("Shared plan generation ")]
    if len(markers) != 1:
        raise ValueError("Successful producer requires one generation marker")
    marker = markers[0]
    match = re.fullmatch(r"Shared plan generation ([1-9][0-9]*)", marker["name"])
    if match is None or marker["status"] != "completed" or marker["conclusion"] != "success":
        raise ValueError("Producer generation marker must have completed successfully")
    generation = int(match[1])
    if generation > view:
        raise ValueError("Producer generation exceeds its native job view")
    return generation


def producer_attempt(jobs: list[dict[str, Any]], run: PlanRun) -> int | None:
    attempts = _producer_versions(jobs, run)
    if not attempts or max(attempts) < run.floor:
        return None
    latest = max(attempts)
    job = attempts[latest]
    status, conclusion = job["status"], job["conclusion"]
    if status in {"queued", "in_progress", "waiting", "pending", "requested"}:
        if conclusion is not None:
            raise ValueError("An incomplete producer cannot have a conclusion")
        return None
    if status != "completed":
        raise ValueError(f"Unknown producer status: {status!r}")
    if conclusion == "success":
        generation = _successful_generation(job, latest)
        return generation if generation >= run.floor else None
    if conclusion not in {
        "failure",
        "cancelled",
        "timed_out",
        "skipped",
        "neutral",
        "action_required",
        "stale",
        "startup_failure",
    }:
        raise ValueError(f"Unknown producer conclusion: {conclusion!r}")
    if latest == run.attempt:
        raise ValueError(f"Shared plan producer ended {conclusion} in attempt {latest}")
    return None


def artifact_id(artifacts: list[dict[str, Any]], run: PlanRun, attempt: int) -> int | None:
    name = f"backend-file-plan-{run.run_id}-{attempt}"
    matches = [item for item in artifacts if item["name"] == name]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Shared plan artifact identity is ambiguous")
    item = matches[0]
    if type(item["id"]) is not int or item["id"] <= 0 or item["expired"] is not False:
        raise ValueError("Shared plan artifact is expired or has an invalid id")
    workflow = item["workflow_run"]
    if (
        type(workflow["id"]) is not int
        or workflow["id"] != run.run_id
        or workflow["head_sha"] != run.source_sha
    ):
        raise ValueError("Shared plan artifact belongs to another run or source")
    return item["id"]


# meiva:note
# Each read-only gh request inherits the CI token and gets only the remaining outer budget.
def _query(endpoint: str, remaining: float) -> object:
    result = subprocess.run(  # noqa: S603
        ["gh", "api", "--paginate", "--slurp", endpoint],
        capture_output=True,
        text=True,
        check=True,
        timeout=min(15.0, remaining),
    )
    return json.loads(result.stdout)


def wait_for_plan(run: PlanRun, timeout: float) -> tuple[int, int]:
    if not 1 <= run.floor <= run.attempt or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Shared plan wait requires valid attempt bounds and a positive timeout")
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        jobs = records(
            _query(
                f"repos/{run.repo}/actions/runs/{run.run_id}/jobs?filter=all&per_page=100",
                remaining,
            ),
            "jobs",
        )
        attempt = producer_attempt(jobs, run)
        if attempt is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            artifacts = records(
                _query(
                    f"repos/{run.repo}/actions/runs/{run.run_id}/artifacts?per_page=100", remaining
                ),
                "artifacts",
            )
            identifier = artifact_id(artifacts, run, attempt)
            if identifier is not None:
                return identifier, attempt
        time.sleep(max(0.0, min(5.0, deadline - time.monotonic())))
    raise TimeoutError("Shared plan producer/artifact did not become ready within the wait budget")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wait for the exact run's successful full-plan input"
    )
    for name in ("repo", "source-sha", "producer", "output", "record"):
        parser.add_argument(f"--{name}", required=True)
    for name in ("run-id", "attempt", "floor"):
        parser.add_argument(f"--{name}", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()
    run = PlanRun(args.repo, args.run_id, args.attempt, args.floor, args.source_sha, args.producer)
    try:
        identifier, attempt = wait_for_plan(run, args.timeout)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print(f"Shared plan input failed: {error}", file=sys.stderr)
        return 1
    Path(args.record).parent.mkdir(parents=True, exist_ok=True)
    Path(args.record).write_text(
        json.dumps(
            {
                "run_id": run.run_id,
                "consumer_attempt": run.attempt,
                "producer_attempt": attempt,
                "artifact_id": identifier,
            }
        )
        + "\n"
    )
    with Path(args.output).open("a") as output:
        output.write(f"artifact_id={identifier}\nproducer_attempt={attempt}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
