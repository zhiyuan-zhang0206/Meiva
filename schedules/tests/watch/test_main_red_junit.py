from __future__ import annotations

import io
import json
import subprocess
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import pytest

from base.clock import Deadline
from base.clock.tests.manual_timers import ManualTimers
from schedules.jobs.watch.main_red_junit import JunitReader
from schedules.plans.watch.junit_cases import CaseOutcome
from schedules.plans.watch.main_red import Entry, RunFacts

WHEN = datetime(2026, 11, 12, tzinfo=UTC)
CASE = "tests/integration/test_schedule_runner_cleanup.py::test_runner_hard_exit_child_ownership[complete]"


def entry() -> Entry:
    return Entry("ci.yml::" + CASE, "suspected", "old", WHEN, "old", WHEN, 1)


def reader(
    tmp_path: Path,
    xml: str,
    *,
    expired: bool = False,
    attempt: int = 1,
    timers: ManualTimers | None = None,
) -> JunitReader:
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as archive:
        archive.writestr(f"junit-backend-shard-3-a{attempt}.xml", xml)

    def run(
        argv: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        stdout: BinaryIO | None = None,
        stderr: int | None = None,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        if stdout is not None:
            stdout.write(contents.getvalue())
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "artifacts": [
                        {"id": 123, "name": "test-results-backend-shard-3", "expired": expired}
                    ]
                }
            ),
            "",
        )

    return JunitReader(tmp_path, run, budget=lambda: Deadline.after(timers or ManualTimers(), 120))


@pytest.mark.parametrize(
    ("child", "expected"),
    [
        ("", CaseOutcome.PASSED),
        ("<skipped/>", CaseOutcome.SKIPPED),
        ("<failure/>", CaseOutcome.FAILED),
    ],
)
def test_real_case_identity_is_found_in_another_shard_of_a_failed_run(
    tmp_path: Path,
    child: str,
    expected: CaseOutcome,
) -> None:
    xml = f"""<testsuite><testcase file="tests/integration/test_schedule_runner_cleanup.py"
        name="test_runner_hard_exit_child_ownership[complete]" time="2.329">{child}</testcase></testsuite>"""
    source = reader(tmp_path, xml)
    run = RunFacts("ci.yml", "new", WHEN, "failure", (), run_id=43126259332)
    observation = source(entry(), run)
    assert observation.outcome == expected
    assert "test-results-backend-shard-3" in observation.detail
    assert "43126259332" in observation.detail
    assert source(entry(), run) == observation
    assert not list(tmp_path.glob("*.zip"))


def test_missing_and_invalid_evidence_stays_unknown(tmp_path: Path) -> None:
    run = RunFacts("ci.yml", "new", WHEN, "success", (), run_id=11)
    assert (
        reader(tmp_path, "<testsuite/>", expired=True)(entry(), run).outcome
        == CaseOutcome.UNREADABLE
    )
    assert reader(tmp_path, "<broken")(entry(), run).outcome == CaseOutcome.UNREADABLE
    assert (
        reader(tmp_path, "<testsuite/>", attempt=2)(entry(), run).outcome == CaseOutcome.UNREADABLE
    )
    absent = reader(tmp_path, "<testsuite/>")(entry(), run)
    assert absent.outcome == CaseOutcome.NOT_COLLECTED
    assert "available JUnit" in absent.detail


def test_partial_lane_cannot_be_retired_by_another_lanes_case_green(tmp_path: Path) -> None:
    xml = '<testsuite><testcase file="tests/integration/test_schedule_runner_cleanup.py" name="test_runner_hard_exit_child_ownership[complete]"/></testsuite>'
    source = reader(tmp_path, xml)
    run = RunFacts("ci.yml", "new", WHEN, "success", (), run_id=11)
    partial = replace(entry(), covers_full_suite=False, job="data plane (macOS)")
    assert source(partial, run).outcome == CaseOutcome.UNREADABLE
    assert source(entry(), run).outcome == CaseOutcome.PASSED


def test_round_budget_is_shared_across_reads_and_cache_survives_expiry(tmp_path: Path) -> None:
    timers = ManualTimers()
    source = reader(tmp_path, "<testsuite/>", timers=timers)
    run = RunFacts("ci.yml", "new", WHEN, "failure", (), run_id=11)
    assert source(entry(), run).outcome == CaseOutcome.NOT_COLLECTED
    timers.advance(121)
    assert source(entry(), run).outcome == CaseOutcome.NOT_COLLECTED
    with pytest.raises(TimeoutError, match="budget exhausted"):
        source(entry(), replace(run, run_id=12))
