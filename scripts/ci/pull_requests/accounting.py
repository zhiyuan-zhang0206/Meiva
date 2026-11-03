#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from base.host.system.hostport import Host
from scripts.boundary.fields import optional_int, optional_object, optional_str
from scripts.boundary.options import options
from scripts.boundary.process import stdout_text
from scripts.boundary.untyped import is_object_dict, is_object_list, object_of

_GH_TIMEOUT_S = 60.0
LINUX_MINUTE_USD = 0.006
MACOS_MINUTE_USD = 0.062
INCLUDED_MINUTES_MONTHLY = 3000

DEFAULT_REPO = "zhiyuan-zhang0206/Meiva"
_LEDGER_DIR = Path(__file__).resolve().parents[1] / "ci_usage"
DEFAULT_LEDGER = _LEDGER_DIR / "ledger.jsonl"

_TASK_RE = re.compile(r"\(task #(\d+)\)")
_TRUNK_PR_RE = re.compile(r"trunk-merge/pr-(\d+)/")

# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
_WINDOW_CHUNK_SECONDS = 4 * 3600


# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
class CiUsageReadError(RuntimeError):
    pass


class LedgerEntry(TypedDict):
    # meiva:note
    # One CI run's attribution and billable minutes: a line of the append-only JSONL ledger.

    run_id: int
    day: str
    agent_id: int | None
    task_id: int | None
    pr_number: int | None
    name: str | None
    head_branch: str | None
    conclusion: str | None
    linux_minutes: int
    macos_minutes: int
    jobs: int


class ReportRow(TypedDict):
    # meiva:note
    # One agent's rollup of the ledger over the report window; `agent_id` None is the unattributed row.

    agent_id: int | None
    runs: int
    linux_minutes: int
    macos_minutes: int
    est_usd: float


@dataclass(frozen=True)
class Options:
    # meiva:note
    # The flags `main` reads, as the parser answered them.

    repo: str
    since: str | None
    until: str | None
    append_ledger: str | None
    report: bool
    days: int
    agent: int | None
    json: bool


# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
def _gh_output(argv: list[str], *, operation: str, host: Host) -> str:
    try:
        result = host.run(argv, capture_output=True, text=True, timeout=_GH_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CiUsageReadError(f"{operation}: GitHub query timed out") from exc
    if result.returncode != 0:
        raise CiUsageReadError(f"{operation}: GitHub query failed (exit {result.returncode})")
    return stdout_text(result)


# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
def _gh_api(path: str, *, host: Host) -> object:
    output = _gh_output(["gh", "api", path], operation=path, host=host)
    try:
        return object_of(json.loads, output)
    except json.JSONDecodeError as exc:
        raise CiUsageReadError(f"{path}: GitHub response is not JSON") from exc


# meiva:note
# The integer `id` a page row was checked to carry.
def _row_id(row: Mapping[str, object]) -> int:
    identifier = optional_int(row, "id")
    if identifier is None:
        raise CiUsageReadError("invalid row identity")
    return identifier


# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
def _read_page(path: str, key: str, *, host: Host) -> tuple[int, list[dict[str, object]]]:
    payload = _gh_api(path, host=host)
    if not is_object_dict(payload):
        raise CiUsageReadError(f"{path}: expected a GitHub page object")
    total, raw_rows = payload.get("total_count"), payload.get(key)
    if type(total) is not int or total < 0 or not is_object_list(raw_rows):
        raise CiUsageReadError(f"{path}: missing or invalid page fields")
    if len(raw_rows) > 100:
        raise CiUsageReadError(f"{path}: inconsistent page count")
    rows: list[dict[str, object]] = []
    for row in raw_rows:
        if not is_object_dict(row) or type(row.get("id")) is not int:
            raise CiUsageReadError(f"{path}: invalid row identity")
        rows.append(row)
    return total, rows


# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
def _pages(
    path: str, key: str, *, host: Host, max_items: int | None = None
) -> list[dict[str, object]]:
    found: dict[int, dict[str, object]] = {}
    expected: int | None = None
    page = 1
    endpoint, _, query = path.partition("?")
    while True:
        total, rows = _read_page(
            f"{endpoint}?per_page=100&page={page}" + (f"&{query}" if query else ""), key, host=host
        )
        if expected is not None and total != expected:
            raise CiUsageReadError(f"{path}: incomplete read; total_count changed while paging")
        # meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
        if max_items is not None and total >= max_items:
            raise CiUsageReadError(
                f"{path}: incomplete read; narrow the window below {max_items} runs"
            )
        expected = total
        for row in rows:
            identifier = _row_id(row)
            if identifier in found:
                raise CiUsageReadError(f"{path}: incomplete read; duplicate row across pages")
            found[identifier] = row
        if len(found) > total:
            raise CiUsageReadError(f"{path}: inconsistent page count")
        if len(found) == total:
            return list(found.values())
        if len(rows) < 100:
            raise CiUsageReadError(f"{path}: incomplete page ({len(found)} of {total} rows)")
        page += 1


def parse_identity(text: str) -> tuple[int | None, int | None]:
    task_match = _TASK_RE.search(text)
    task_id = int(task_match.group(1)) if task_match else None
    return None, task_id


def _pr_title(repo: str, pr_number: int | None, *, host: Host) -> str:
    if pr_number is None:
        return ""
    return _gh_output(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "title", "--jq", ".title"],
        operation=f"PR {pr_number} title",
        host=host,
    ).strip()


def _real_pr_of_trunk_branch(head_ref: str) -> int | None:
    match = _TRUNK_PR_RE.search(head_ref or "")
    return int(str(match.group(1))) if match else None


# meiva:note
# A job's billable minutes are the ceiling of its wall duration. The jobs API reports null started/completed for in-progress and cancelled-before-start jobs; those pass through as absent and bill 0 minutes rather than crashing the timestamp parse.
# meiva:link scripts/ci/pull_requests/docs/accounting.meiva.okf.md
def job_minutes(started: str | None, completed: str | None) -> int:
    # meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
    if started is None or completed is None:
        return 0
    start = datetime.fromisoformat(started.replace("Z", "+00:00"))
    end = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    if start.utcoffset() is None or end.utcoffset() is None or end < start:
        raise ValueError("job timestamps must be timezone-aware and ordered")
    return math.ceil((end - start).total_seconds() / 60.0)


# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
# meiva:note
# The `number` of each pull request in `raw`; an entry without an integer one is `error`.
def _pull_request_numbers(raw: list[object], error: str) -> list[int]:
    numbers: list[int] = []
    for pr in raw:
        number = optional_int(pr, "number") if is_object_dict(pr) else None
        if number is None:
            raise CiUsageReadError(error)
        numbers.append(number)
    return numbers


# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
def _identity_from_prs(
    run: Mapping[str, object], repo: str, real_pr: int | None, *, host: Host
) -> tuple[int | None, int | None, int | None]:
    raw_pull_requests: object = run.get("pull_requests")
    if not is_object_list(raw_pull_requests):
        raise CiUsageReadError("workflow run: invalid pull_requests")
    pull_requests = _pull_request_numbers(raw_pull_requests, "workflow run: invalid pull_requests")
    if pull_requests:
        pr_number = pull_requests[0]
        title = _pr_title(repo, real_pr if real_pr else pr_number, host=host)
        agent_id, task_id = parse_identity(title)
        return (real_pr or pr_number), agent_id, task_id
    if real_pr is not None:
        agent_id, task_id = parse_identity(_pr_title(repo, real_pr, host=host))
        return real_pr, agent_id, task_id
    return None, None, None


def _merged_pr_of(run: Mapping[str, object], repo: str, *, host: Host) -> int | None:
    head_sha = str(run.get("head_sha") or "")
    if not head_sha:
        return None
    raw_merged_prs = _gh_api(f"repos/{repo}/commits/{head_sha}/pulls", host=host)
    if not is_object_list(raw_merged_prs):
        raise CiUsageReadError(f"commit {head_sha}: invalid pull request associations")
    merged_prs = _pull_request_numbers(
        raw_merged_prs, f"commit {head_sha}: invalid pull request associations"
    )
    return merged_prs[0] if merged_prs else None


# meiva:note
# A run's PR and task identity resolves in order: the real PR of a synthetic `trunk-merge/pr-<n>/<uuid>` branch (or the run's PR payload) by PR title; then a push-to-main merge run's merged PR via its merge commit; then the head commit subject for `(task #<id>)`. Agent prefix/branch matching is not performed.
# meiva:link scripts/ci/pull_requests/docs/accounting.meiva.okf.md
def _run_identity(
    # meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
    run: Mapping[str, object],
    repo: str,
    *,
    host: Host,
) -> tuple[int | None, int | None, int | None]:
    real_pr = _real_pr_of_trunk_branch(str(run.get("head_branch") or ""))
    pr_number, agent_id, task_id = _identity_from_prs(run, repo, real_pr, host=host)
    if pr_number is None and str(run.get("head_branch") or "") == "main":
        merged = _merged_pr_of(run, repo, host=host)
        if merged is not None:
            pr_number = merged
            agent_id, task_id = parse_identity(_pr_title(repo, pr_number, host=host))
    if task_id is None:
        head_commit = optional_object(run, "head_commit") or {}
        _, task_id = parse_identity(str(head_commit.get("message") or ""))
    return pr_number, agent_id, task_id


def _run_entry(run: Mapping[str, object], repo: str, *, host: Host) -> LedgerEntry:
    pr_number, agent_id, task_id = _run_identity(run, repo, host=host)
    day = (optional_str(run, "created_at") or "")[:10]
    return LedgerEntry(
        run_id=_row_id(run),
        day=day,
        agent_id=agent_id,
        task_id=task_id,
        pr_number=pr_number,
        name=optional_str(run, "name"),
        head_branch=optional_str(run, "head_branch"),
        conclusion=optional_str(run, "conclusion"),
        linux_minutes=0,
        macos_minutes=0,
        jobs=0,
    )


# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
def _completed_job_minutes(job: Mapping[str, object]) -> int:
    conclusion = job.get("conclusion")
    if job.get("status") != "completed" or not isinstance(conclusion, str):
        raise CiUsageReadError(f"job {job['id']}: incomplete; job is not completed")
    if "started_at" not in job or "completed_at" not in job:
        raise CiUsageReadError(f"job {job['id']}: missing completed-job timestamps")
    started, completed = job["started_at"], job["completed_at"]
    if started is None and conclusion in {"cancelled", "skipped"}:
        return 0
    if not isinstance(started, str) or not isinstance(completed, str):
        raise CiUsageReadError(f"job {job['id']}: incomplete completed-job timestamps")
    try:
        return job_minutes(started, completed)
    except ValueError as exc:
        raise CiUsageReadError(f"job {job['id']}: invalid completed-job timestamps") from exc


def _fill_job_minutes(entry: LedgerEntry, repo: str, *, host: Host) -> None:
    jobs = _pages(f"repos/{repo}/actions/runs/{entry['run_id']}/jobs", "jobs", host=host)
    for job in jobs:
        minutes = _completed_job_minutes(job)
        name = job.get("name")
        if not isinstance(name, str):
            raise CiUsageReadError(f"job {job['id']}: missing job name")
        if "macos" in name.lower():
            entry["macos_minutes"] += minutes
        else:
            entry["linux_minutes"] += minutes
        entry["jobs"] += 1


# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
def collect(repo: str, since: str, until: str, *, host: Host) -> list[LedgerEntry]:
    entries: dict[int, dict[str, object]] = {}
    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
    cursor = since_dt
    while cursor < until_dt:
        chunk_end = min(cursor.timestamp() + _WINDOW_CHUNK_SECONDS, until_dt.timestamp())
        window = (
            datetime.fromtimestamp(cursor.timestamp(), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            + ".."
            + datetime.fromtimestamp(chunk_end, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        runs = _pages(
            f"repos/{repo}/actions/runs?created={window}",
            "workflow_runs",
            host=host,
            max_items=1000,
        )
        for run in runs:
            name = run.get("name")
            if not isinstance(name, str) or not name.strip():
                raise CiUsageReadError(f"run {run['id']}: invalid workflow name")
            if name != "CI":
                continue
            created_text = run.get("created_at")
            if not isinstance(created_text, str):
                raise CiUsageReadError(f"run {run['id']}: invalid created_at")
            try:
                created = datetime.fromisoformat(created_text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise CiUsageReadError(f"run {run['id']}: invalid created_at") from exc
            if not since_dt <= created < until_dt:
                continue
            if run.get("status") != "completed" or not isinstance(run.get("conclusion"), str):
                raise CiUsageReadError(f"run {run['id']}: incomplete; workflow is not completed")
            entries[_row_id(run)] = run
        cursor = datetime.fromtimestamp(chunk_end, tz=UTC)
    out: list[LedgerEntry] = []
    for run in entries.values():
        entry = _run_entry(run, repo, host=host)
        _fill_job_minutes(entry, repo, host=host)
        out.append(entry)
    return out


def load_ledger(path: Path) -> dict[int, dict[str, object]]:
    if not path.exists():
        return {}
    ledger: dict[int, dict[str, object]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            loaded = object_of(json.loads, line)
        except json.JSONDecodeError:
            continue
        if is_object_dict(loaded) and (run_id := optional_int(loaded, "run_id")) is not None:
            ledger[run_id] = loaded
    return ledger


# meiva:note
# append_ledger keys rows by run id: entries already present are skipped, so re-running a window that overlaps the ledger appends only new runs and returns the appended count (a re-fire over the same window appends 0).
# meiva:link scripts/ci/pull_requests/docs/accounting.meiva.okf.md
def append_ledger(path: Path, entries: Sequence[LedgerEntry]) -> int:
    # meiva:note
    # The JSONL ledger remains append-only and idempotent by run_id. Collection finishes before append begins, so a failed collection leaves existing bytes unchanged and does not append earlier successful rows from that collection. Existing historical rows are not automatically repaired or replaced.
    # meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
    ledger = load_ledger(path)
    fresh = [e for e in entries if e["run_id"] not in ledger]
    if not fresh:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for entry in sorted(fresh, key=lambda e: e["run_id"]):
            fh.write(json.dumps(entry) + "\n")
    return len(fresh)


# meiva:note
# report_rows rolls up the last `days` per agent (runs and linux/macos minutes) and prices them at the GitHub-hosted private-repo overage rates (Linux $0.006/min, macOS $0.062/min; corrected 2026-09-06 per GitHub's published pricing). The repo is currently public, so est_usd is a private-repo overage equivalent — phase-2 quotas must gate on MINUTES, not dollars; the account's 3,000 included minutes/month are account-wide across all repos.
# meiva:link scripts/ci/pull_requests/docs/accounting.meiva.okf.md
def report_rows(
    ledger: Mapping[int, Mapping[str, object]],
    *,
    days: int,
    agent: int | None,
    now: datetime,
) -> list[ReportRow]:
    cutoff = now.timestamp() - days * 86400
    per_agent: dict[int | None, ReportRow] = {}
    for entry in ledger.values():
        if agent is not None and entry.get("agent_id") != agent:
            continue
        try:
            created = datetime.fromisoformat(
                str(entry.get("day") or "") + "T00:00:00+00:00"
            ).timestamp()
        except ValueError:
            created = 0.0
        if created < cutoff:
            continue
        key = optional_int(entry, "agent_id")
        if key not in per_agent:
            per_agent[key] = ReportRow(
                agent_id=key, runs=0, linux_minutes=0, macos_minutes=0, est_usd=0.0
            )
        row = per_agent[key]
        row["runs"] += 1
        row["linux_minutes"] += optional_int(entry, "linux_minutes") or 0
        row["macos_minutes"] += optional_int(entry, "macos_minutes") or 0
    rows = list(per_agent.values())
    for row in rows:
        row["est_usd"] = round(
            row["linux_minutes"] * LINUX_MINUTE_USD + row["macos_minutes"] * MACOS_MINUTE_USD,
            2,
        )
    return sorted(rows, key=lambda r: -r["est_usd"])


def _print_report(rows: Sequence[ReportRow], days: int) -> None:
    print(
        f"CI usage, last {days} days (minutes primary; est. private-repo overage at "
        f"Linux ${LINUX_MINUTE_USD}/min, macOS ${MACOS_MINUTE_USD}/min):"
    )
    if not rows:
        print("No attributed runs.")
        return
    for row in rows:
        agent = row["agent_id"] if row["agent_id"] is not None else "unattributed"
        print(
            f"  #{agent}: {row['runs']} runs, {row['linux_minutes']} linux + "
            f"{row['macos_minutes']} macos minutes, est ${row['est_usd']}"
        )


def _report(days: int, agent: int | None, *, as_json: bool, now: datetime) -> None:
    rows = report_rows(load_ledger(DEFAULT_LEDGER), days=days, agent=agent, now=now)
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        _print_report(rows, days)


def _print_entries(entries: Sequence[LedgerEntry]) -> None:
    if not entries:
        print("No CI runs in the window.")
        return
    for entry in entries:
        agent = entry["agent_id"] if entry["agent_id"] is not None else "unattributed"
        print(
            f"run {entry['run_id']} day={entry['day']} #{agent} "
            f"(task {entry['task_id'] if entry['task_id'] is not None else '-'}) "
            f"pr={entry['pr_number'] if entry['pr_number'] is not None else '-'} "
            f"{entry['linux_minutes']}L+{entry['macos_minutes']}M min "
            f"[{entry['conclusion']}]"
        )


# meiva:link scripts/ci/pull_requests/docs/ci-usage.meiva.okf.md
def main(argv: list[str], *, host: Host) -> int:
    parser = argparse.ArgumentParser(description="CI usage attribution and ledger")
    parser.add_argument(
        "--repo", default=DEFAULT_REPO, help=f"owner/repo (default: {DEFAULT_REPO})"
    )
    parser.add_argument("--since", help="ISO start (e.g. 2026-09-05T16:00Z)")
    parser.add_argument("--until", help="ISO end (exclusive)")
    parser.add_argument("--append-ledger", help="append entries to this JSONL ledger idempotently")
    parser.add_argument("--report", action="store_true", help="per-agent rollup from a ledger")
    parser.add_argument("--days", type=int, default=7, help="with --report: window in days")
    parser.add_argument(
        "--agent", type=int, default=None, help="with --report: restrict to one agent"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = options(parser.parse_args(argv), Options)

    if args.report:
        if args.since or args.until or args.append_ledger:
            parser.error("--report is exclusive with --since/--until/--append-ledger")
        _report(args.days, args.agent, as_json=args.json, now=datetime.now(UTC))
        return 0

    if not args.since or not args.until:
        parser.error("--since and --until are required without --report")
    try:
        entries = collect(args.repo, args.since, args.until, host=host)
    except CiUsageReadError as exc:
        print(f"CI usage collection failed: {exc}", file=sys.stderr)
        return 1
    if args.append_ledger:
        appended = append_ledger(Path(args.append_ledger), entries)
        print(f"appended {appended} entry(s) to {args.append_ledger} (of {len(entries)} collected)")
        return 0
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    _print_entries(entries)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], host=Host.system(environ=os.environ)))
