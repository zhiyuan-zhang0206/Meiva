from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import NotRequired, TypedDict

from base.host.proc import CommandRunner
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import Reply, fake_host
from scripts.ci.pull_requests.status import CIStatus, LimboRun

HEAD = "deadbeef"
NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


@dataclass(frozen=True)
class Evidence:
    view: Reply
    incomplete: Reply = field(default_factory=lambda: Reply(stdout="[]"))
    main: Reply = field(default_factory=lambda: Reply(stdout="1"))
    queued: Reply = field(default_factory=lambda: Reply(stdout="[]"))
    jobs: dict[int, int] = field(default_factory=dict)


def check(
    name: str, conclusion: str, *, state: str = "COMPLETED", workflow: str = "CI"
) -> dict[str, str]:
    return {"name": name, "conclusion": conclusion, "status": state, "workflowName": workflow}


def rollup(
    checks: Sequence[Mapping[str, object]],
    *,
    draft: bool = False,
    mergeable: str = "MERGEABLE",
    scheduled: list[str] | None = None,
    stuck: list[LimboRun] | None = None,
) -> Evidence:
    candidates = [
        {
            "id": run["id"],
            "name": run["name"],
            "created_at": (NOW - timedelta(seconds=run["age_s"])).isoformat(),
        }
        for run in stuck or []
    ]
    return Evidence(
        Reply(
            stdout=json.dumps(
                {
                    "mergeable": mergeable,
                    "headRefOid": HEAD,
                    "isDraft": draft,
                    "statusCheckRollup": checks,
                }
            )
        ),
        incomplete=Reply(stdout=json.dumps(scheduled or [])),
        queued=Reply(stdout=json.dumps(candidates)),
        jobs={run["id"]: 0 for run in stuck or []},
    )


def probe(verdict: CIStatus) -> Evidence:
    if verdict is CIStatus.ERROR:
        return Evidence(Reply(returncode=1, stderr="gh CLI error: boom"))
    if verdict is CIStatus.NO_CHECKS:
        return rollup([])
    if verdict is CIStatus.MERGE_CONFLICT:
        return rollup([], mergeable="CONFLICTING")
    if verdict is CIStatus.NO_WORKFLOW_RUNS:
        return rollup([check("app", "SUCCESS", workflow="")])
    if verdict is CIStatus.NOT_READY:
        return rollup([check("backend shard (1/16)", "SKIPPED")], draft=True)
    if verdict is CIStatus.PENDING:
        return rollup([check("CI", "", state="IN_PROGRESS")])
    if verdict is CIStatus.FAILED:
        return rollup([check("lint", "FAILURE")])
    if verdict is CIStatus.ALL_PASSED:
        return rollup([check("lint", "SUCCESS"), check("test", "SUCCESS")])
    raise AssertionError(verdict)


def _probe_reply(evidence: Evidence, args: list[str]) -> Reply | None:
    if len(args) < 3 or args[1] != "api":
        return None
    endpoint = args[2]
    if "/actions/runs?head_sha=" in endpoint:
        assert f"head_sha={HEAD}" in endpoint
        if "status=completed" in endpoint:
            return evidence.main
        if "{id, name, created_at}" in args[-1]:
            return evidence.queued
        assert 'select(.status != "completed")' in args[-1]
        return evidence.incomplete
    if "/actions/runs/" in endpoint and endpoint.endswith("/jobs"):
        return Reply(stdout=str(evidence.jobs[int(endpoint.split("/")[-2])]))
    if endpoint.endswith(("/timeline", "/commits")):
        return Reply(stdout="null")
    return None


def monitor_host(
    *evidence: Evidence,
    calls: list[list[str]] | None = None,
    other: CommandRunner | None = None,
) -> Host:
    assert evidence
    index = 0
    current = evidence[0]

    def run(
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal index, current
        args = list(argv)
        if calls is not None:
            calls.append(args)
        if "mergeable,statusCheckRollup,headRefOid,isDraft" in args:
            current = evidence[min(index, len(evidence) - 1)]
            index += 1
            reply = current.view
        else:
            reply = _probe_reply(current, args)
            if reply is None:
                if other is not None:
                    return other(
                        argv, timeout=timeout, capture_output=capture_output, input=input, **kwargs
                    )
                if "baseRefOid" in args:
                    reply = Reply(stdout="null")
                else:
                    raise AssertionError(f"unscripted command: {args}")
        return subprocess.CompletedProcess(
            args, reply.returncode, str(reply.stdout), str(reply.stderr)
        )

    return fake_host(run)


class RerunJob(TypedDict):
    name: str
    job_id: int
    run_id: int
    conclusion: str
    run_status: NotRequired[str]


def rerun_host(
    jobs: list[RerunJob],
    *,
    post_error: str = "",
    query_error: bool = False,
    calls: list[list[str]] | None = None,
) -> Host:
    runs: dict[int, str] = {}
    for job in jobs:
        runs[job["run_id"]] = job.get("run_status", "completed")

    def run(argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        if calls is not None:
            calls.append(args)
        if args[:3] == ["gh", "pr", "view"]:
            assert "headRefOid" in args
            return subprocess.CompletedProcess(args, 0, HEAD, "")
        if query_error:
            return subprocess.CompletedProcess(args, 1, "", "gh: rate limited")
        if "--method" in args:
            assert args[args.index("--method") + 1] == "POST"
            return subprocess.CompletedProcess(args, 1 if post_error else 0, "", post_error)
        endpoint = args[2]
        if "/actions/runs?" in endpoint:
            assert f"head_sha={HEAD}" in endpoint
            payload = {
                "workflow_runs": [
                    {
                        "id": run_id,
                        "name": f"workflow-{run_id}",
                        "created_at": NOW.isoformat(),
                        "status": state,
                    }
                    for run_id, state in runs.items()
                ]
            }
        elif "/jobs?" in endpoint:
            run_id = int(endpoint.split("/")[-2])
            payload = {
                "jobs": [
                    {
                        "id": job["job_id"],
                        "name": job["name"],
                        "run_id": run_id,
                        "conclusion": job["conclusion"],
                    }
                    for job in jobs
                    if job["run_id"] == run_id
                ]
            }
        else:
            raise AssertionError(f"unscripted command: {args}")
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    return fake_host(run)
