from __future__ import annotations

import pytest

from base.host.net.tests.fake_url_opener import FakeUrlOpener
from base.host.system.tests.fake_commands import fake_host
from scripts.ci.pull_requests import commands as ci_utils
from scripts.ci.pull_requests.tests.ci_evidence import rerun_host
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    TEST_CLOCK,
)


def test_rerun_failed_jobs_dry_run_lists_jobs(capsys: pytest.CaptureFixture[str]) -> None:
    host = rerun_host(
        [
            {
                "name": "e2e shard (3/4)",
                "job_id": 102,
                "run_id": 11,
                "conclusion": "FAILURE",
                "run_status": "completed",
            }
        ],
        post_error="",
        query_error=False,
    )
    assert (
        ci_utils.main(
            ["42", "--rerun-failed-jobs", "--dry-run"],
            clock=TEST_CLOCK,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "e2e shard (3/4)" in out and "102" in out and "[run completed]" in out


def test_rerun_failed_jobs_nothing_to_do(capsys: pytest.CaptureFixture[str]) -> None:
    host = rerun_host([], post_error="", query_error=False)
    assert (
        ci_utils.main(
            ["42", "--rerun-failed-jobs", "--dry-run"],
            clock=TEST_CLOCK,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 0
    )
    assert "No failed jobs" in capsys.readouterr().out


def test_rerun_failed_jobs_forwards_and_reports_errors(capsys: pytest.CaptureFixture[str]) -> None:
    host = rerun_host(
        [{"name": "lint", "job_id": 104, "run_id": 11, "conclusion": "failure"}],
        post_error="gh: rate limited",
        query_error=False,
    )
    assert (
        ci_utils.main(
            ["42", "--rerun-failed-jobs"],
            clock=TEST_CLOCK,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 3
    )
    assert "Re-run failed: lint: gh: rate limited" in capsys.readouterr().out


def test_rerun_failed_jobs_success_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    host = rerun_host(
        [{"name": "lint", "job_id": 104, "run_id": 11, "conclusion": "FAILURE"}],
        post_error="",
        query_error=False,
    )
    assert (
        ci_utils.main(
            ["42", "--rerun-failed-jobs"],
            clock=TEST_CLOCK,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 0
    )
    assert "Re-ran lint" in capsys.readouterr().out


def test_rerun_failed_jobs_waits_for_still_running_run(capsys: pytest.CaptureFixture[str]) -> None:
    # meiva:note
    # Failures waiting on a still-running run are reported with the recovery
    # action and exit 5, never forwarded as a 403 (task #3764).
    host = rerun_host(
        [
            {
                "name": "backend shard (8/16)",
                "job_id": 105093815208,
                "run_id": 35187743815,
                "conclusion": "failure",
                "run_status": "in_progress",
            }
        ],
        post_error="",
        query_error=False,
    )
    assert (
        ci_utils.main(
            ["42", "--rerun-failed-jobs"],
            clock=TEST_CLOCK,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 5
    )
    out = capsys.readouterr().out
    assert "Waiting: backend shard (8/16)" in out
    assert "in_progress" in out
    assert "Re-run this command once the run finishes." in out


def test_rerun_failed_jobs_reports_mixed_outcomes(capsys: pytest.CaptureFixture[str]) -> None:
    # meiva:note
    # A rerun that partly lands and partly waits prints both lines and exits
    # 5: the waiting remainder governs the exit (task #3764).
    host = rerun_host(
        [
            {"name": "lint", "job_id": 104, "run_id": 11, "conclusion": "FAILURE"},
            {
                "name": "backend shard (8/16)",
                "job_id": 105093815208,
                "run_id": 12,
                "conclusion": "failure",
                "run_status": "in_progress",
            },
        ],
        post_error="",
        query_error=False,
    )
    assert (
        ci_utils.main(
            ["42", "--rerun-failed-jobs"],
            clock=TEST_CLOCK,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 5
    )
    out = capsys.readouterr().out
    assert "Re-ran lint" in out
    assert "Waiting: backend shard (8/16)" in out


def test_rerun_failed_jobs_query_failure_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    # meiva:note
    # A failed GitHub query must not read as "no failed jobs" (issue #1945):
    # the error is reported on stderr and the exit code flips to 1.
    host = rerun_host([], post_error="", query_error=True)

    assert (
        ci_utils.main(
            ["42", "--rerun-failed-jobs", "--dry-run"],
            clock=TEST_CLOCK,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "Failed to list failed jobs" in captured.err
    assert "No failed jobs" not in captured.out


def test_rerun_failed_jobs_query_failure_non_dry_run_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = rerun_host([], post_error="", query_error=True)

    assert (
        ci_utils.main(
            ["42", "--rerun-failed-jobs"],
            clock=TEST_CLOCK,
            host=host,
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
        == 1
    )
    assert "Failed to list failed jobs" in capsys.readouterr().err


def test_rerun_failed_jobs_exclusive_with_wait() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(
            ["42", "--rerun-failed-jobs", "--wait"],
            clock=TEST_CLOCK,
            host=fake_host(),
            open_url=FakeUrlOpener(),
            workflows=True,
            env={},
        )
