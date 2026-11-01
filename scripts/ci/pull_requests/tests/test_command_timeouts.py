from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.host.net.tests.fake_url_opener import FakeUrlOpener
from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host
from scripts.ci.pull_requests import (
    commands,
    job_rerun,
    status,
)


def _clock() -> Clock:
    timers = ManualTimers(wall_start=datetime(2026, 10, 1, tzinfo=UTC))
    return Clock(
        ClockConfig(timezone="UTC", authoritative_timezone="UTC", message_timestamp_weekday=False),
        timers=timers,
        now=timers.now,
    )


def _rollup(*, empty: bool = False) -> Reply:
    return Reply(
        stdout=json.dumps(
            {
                "mergeable": "MERGEABLE",
                "state": "OPEN",
                "headRefOid": "abc123",
                "statusCheckRollup": []
                if empty
                else [
                    {
                        "__typename": "CheckRun",
                        "name": "backend",
                        "workflowName": "CI",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    }
                ],
            }
        )
    )


@pytest.mark.parametrize("probe", ["rollup", "incomplete", "empty", "completed"])
def test_status_timeout_never_claims_green_or_no_checks(probe: str) -> None:
    runner = FakeCommands()
    error = subprocess.TimeoutExpired("gh", 30)
    runner.on(["gh", "pr", "view"], error if probe == "rollup" else _rollup(empty=probe == "empty"))
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs?head_sha=abc123"],
        Reply(stdout="[]") if probe == "completed" else error,
    )
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs?head_sha=abc123&status=completed&per_page=100"], error
    )
    result = status.check_ci(
        "42", repo="o/r", host=fake_host(runner), clock=_clock(), workflows=True
    )
    assert result.verdict is status.CIStatus.ERROR
    assert result.error_detail


def test_wait_reports_three_command_timeouts_as_query_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeCommands()
    runner.on(["gh", "pr", "view"], subprocess.TimeoutExpired("gh", 30), repeat=True)
    result = commands.main(
        ["42", "--repo", "o/r", "--wait", "--every", "1"],
        host=fake_host(runner),
        clock=_clock(),
        open_url=FakeUrlOpener(),
        workflows=True,
        env={},
    )
    assert result == 3
    assert len(runner.calls) == status.MAX_CONSECUTIVE_ERRORS
    assert "timed out" in capsys.readouterr().err


@pytest.mark.parametrize("probe", ["runs", "jobs"])
def test_limbo_timeout_remains_missing_evidence(probe: str) -> None:
    runner = FakeCommands()
    error = subprocess.TimeoutExpired("gh", 30)
    candidates = Reply(
        stdout=json.dumps([{"id": 91, "name": "CI", "created_at": "2026-09-01T00:00:00Z"}])
    )
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs?head_sha=abc123"],
        error if probe == "runs" else candidates,
    )
    runner.on(["gh", "api", "repos/o/r/actions/runs/91/jobs"], error)
    result = status.limbo_runs("abc123", "o/r", host=fake_host(runner), clock=_clock())
    assert result is None if probe == "runs" else result == []


@pytest.mark.parametrize("probe", ["head", "runs", "jobs"])
def test_rerun_query_timeout_retains_its_cause(probe: str) -> None:
    runner = FakeCommands()
    error = subprocess.TimeoutExpired("gh", 30)
    runner.on(["gh", "pr", "view"], error if probe == "head" else Reply(stdout="abc123"))
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs?head_sha=abc123&per_page=100"],
        error
        if probe == "runs"
        else Reply(
            stdout=json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 1,
                            "name": "CI",
                            "created_at": "2026-10-01T00:00:00Z",
                            "status": "completed",
                        }
                    ]
                }
            )
        ),
    )
    runner.on(["gh", "api", "repos/o/r/actions/runs/1/jobs?per_page=100"], error)
    with pytest.raises(job_rerun.CiJobRerunError, match="timed out") as caught:
        job_rerun.list_failed_jobs("42", "o/r", host=fake_host(runner))
    assert caught.value.__cause__ is error
