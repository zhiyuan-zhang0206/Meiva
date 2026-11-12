from __future__ import annotations

import io
import json
import subprocess
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import pytest

from base.clock import Deadline
from base.clock.tests.manual_timers import ManualTimers
from schedules.jobs.watch.main_red_junit import JunitReader
from schedules.plans.watch.junit_cases import CaseOutcome
from schedules.plans.watch.junit_publications import current_publications
from schedules.plans.watch.main_red import Entry, RunFacts

START = datetime(2026, 11, 12, tzinfo=UTC)
CASE = "tests/integration/test_retry.py::test_retry"
PASS = (
    '<testsuite><testcase classname="tests.integration.test_retry" name="test_retry"/></testsuite>'
)
FAIL = PASS.replace("/></testsuite>", "><failure/></testcase></testsuite>")


@dataclass(frozen=True)
class Bundle:
    name: str
    created_at: str | None
    documents: dict[str, str]


def source(
    tmp_path: Path,
    bundles: tuple[Bundle, ...],
    *,
    attempt: int = 1,
    started: str | None = START.isoformat(),
) -> tuple[JunitReader, list[int]]:
    downloads: list[int] = []

    def run(
        argv: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        stdout: BinaryIO | None = None,
        stderr: int | None = None,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        if stdout is not None:
            identifier = int(argv[-1].split("/")[-2])
            downloads.append(identifier)
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                for name, xml in bundles[identifier - 1].documents.items():
                    archive.writestr(name, xml)
            _ = stdout.write(data.getvalue())
            answer = ""
        elif "/artifacts?" in argv[-1]:
            answer = json.dumps(
                {
                    "artifacts": [
                        {"id": i + 1, "name": b.name, "created_at": b.created_at}
                        for i, b in enumerate(bundles)
                    ]
                }
            )
        else:
            answer = json.dumps({"run_attempt": attempt, "run_started_at": started})
        return subprocess.CompletedProcess(argv, 0, answer, "")

    return JunitReader(tmp_path, run, budget=lambda: Deadline.after(ManualTimers(), 120)), downloads


def facts(*, attempt: int = 1) -> RunFacts:
    return RunFacts("ci.yml", "new", START, "success", (), run_id=11, attempt=attempt)


def entry() -> Entry:
    return Entry("ci.yml::" + CASE, "suspected", "old", START, "old", START, 1)


def test_internal_a2_is_read_inside_github_attempt_one(tmp_path: Path) -> None:
    reader, downloads = source(
        tmp_path,
        (
            Bundle(
                "test-results-backend-shard-3",
                START.isoformat(),
                {"junit-backend-shard-3-a2.xml": PASS},
            ),
        ),
    )
    assert reader(entry(), facts()).outcome == CaseOutcome.PASSED
    assert downloads == [1]


def test_same_named_artifact_from_older_workflow_attempt_is_unknown(tmp_path: Path) -> None:
    reader, downloads = source(
        tmp_path,
        (
            Bundle(
                "test-results-backend-shard-3",
                START.isoformat(),
                {"junit-backend-shard-3-a2.xml": PASS},
            ),
        ),
        attempt=2,
        started=(START + timedelta(hours=1)).isoformat(),
    )
    observation = reader(entry(), facts(attempt=2))
    assert observation.outcome == CaseOutcome.UNREADABLE
    assert "cannot be tied" in observation.detail
    assert downloads == []


@pytest.mark.parametrize(
    ("attempt", "created", "started"),
    [
        (3, START.isoformat(), START.isoformat()),
        (2, None, START.isoformat()),
        (2, START.isoformat(), None),
    ],
)
def test_unproved_workflow_ownership_is_never_green(
    tmp_path: Path, attempt: int, created: str | None, started: str | None
) -> None:
    reader, downloads = source(
        tmp_path,
        (Bundle("verdict-junit-backend-3", created, {"backend-3.xml": PASS}),),
        attempt=attempt,
        started=started,
    )
    assert reader(entry(), facts(attempt=2)).outcome == CaseOutcome.UNREADABLE
    assert not downloads


def test_later_internal_retry_supersedes_only_cases_it_contains(tmp_path: Path) -> None:
    older = FAIL.replace(
        "</testsuite>",
        '<testcase file="tests/test_other.py" name="test_other"><failure/></testcase></testsuite>',
    )
    reader, _ = source(
        tmp_path,
        (
            Bundle(
                "test-results-backend-shard-3",
                START.isoformat(),
                {"junit-backend-shard-3-a1.xml": older, "junit-backend-shard-3-a2.xml": PASS},
            ),
        ),
    )
    assert reader(entry(), facts()).outcome == CaseOutcome.PASSED
    other = replace(entry(), fingerprint="ci.yml::tests/test_other.py::test_other")
    assert reader(other, facts()).outcome == CaseOutcome.FAILED


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("verdict_passes", [False, True])
def test_final_verdict_wins_over_raw_retry_results(
    tmp_path: Path, reverse: bool, partial: bool, verdict_passes: bool
) -> None:
    bundles = (
        Bundle(
            "test-results-backend-shard-3",
            START.isoformat(),
            {"raw-a2.xml": FAIL if verdict_passes else PASS},
        ),
        Bundle(
            "verdict-junit-backend-3",
            START.isoformat(),
            {"backend-3.xml": PASS if verdict_passes else FAIL},
        ),
    )
    reader, _ = source(tmp_path, tuple(reversed(bundles)) if reverse else bundles)
    target = replace(entry(), covers_full_suite=not partial, job="Backend shard (3/8)")
    assert reader(target, facts()).outcome == (
        CaseOutcome.PASSED if verdict_passes else CaseOutcome.FAILED
    )


def test_conflicting_results_without_retry_order_stay_unknown(tmp_path: Path) -> None:
    reader, _ = source(
        tmp_path,
        (Bundle("verdict-junit-backend-3", START.isoformat(), {"one.xml": PASS, "two.xml": FAIL}),),
    )
    assert reader(entry(), facts()).outcome == CaseOutcome.UNREADABLE


@pytest.mark.parametrize("reverse", [False, True])
def test_current_publication_selection_excludes_same_named_older_artifacts(reverse: bool) -> None:
    name = "test-results-backend-shard-4"
    cutoff = datetime.fromisoformat("2026-11-12T02:00:58+00:00")
    records = [
        {"name": name, "id": 13908093071, "created_at": "2026-11-12T02:00:27Z"},
        {"name": name, "id": 13908024517, "created_at": "2026-11-12T02:08:09Z"},
    ]
    selected, errors = current_publications(
        tuple(reversed(records)) if reverse else records, cutoff
    )
    assert selected == {name: 13908024517}
    assert not errors
    selected, errors = current_publications(records[:1], cutoff)
    assert not selected and errors


def test_identical_publication_timestamps_with_different_ids_are_ambiguous() -> None:
    records = [
        {"name": "verdict-junit-e2e-3", "id": i, "created_at": START.isoformat()} for i in (1, 2)
    ]
    selected, errors = current_publications(records, START)
    assert not selected
    assert "ambiguous" in errors[0]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("problem", ["unverified", "malformed", "ambiguous"])
@pytest.mark.parametrize("partial", [False, True])
def test_raw_pass_cannot_mask_unreadable_final_publication(
    tmp_path: Path, problem: str, reverse: bool, partial: bool
) -> None:
    verdict = Bundle(
        "verdict-junit-backend-3",
        None if problem == "unverified" else START.isoformat(),
        {"backend-3.xml": "<broken" if problem == "malformed" else PASS},
    )
    bundles = (
        Bundle("test-results-backend-shard-3", START.isoformat(), {"raw-a2.xml": PASS}),
        verdict,
    )
    if problem == "ambiguous":
        bundles = (*bundles, verdict)
    reader, _ = source(tmp_path, tuple(reversed(bundles)) if reverse else bundles)
    target = replace(entry(), covers_full_suite=not partial, job="Backend full suite (3/8)")
    observation = reader(target, facts())
    assert observation.outcome == CaseOutcome.UNREADABLE
    assert "verdict-junit-backend-3" in observation.detail
    assert {
        "unverified": "cannot be tied",
        "malformed": "unreadable JUnit",
        "ambiguous": "ambiguous publications",
    }[problem] in observation.detail


def test_partial_lane_ignores_an_unreadable_other_lane(tmp_path: Path) -> None:
    reader, _ = source(
        tmp_path,
        (
            Bundle("test-results-backend-shard-3", START.isoformat(), {"raw-a2.xml": PASS}),
            Bundle("verdict-junit-e2e-3", None, {"e2e-3.xml": "<broken"}),
        ),
    )
    target = replace(entry(), covers_full_suite=False, job="Backend full suite (3/8)")
    assert reader(target, facts()).outcome == CaseOutcome.PASSED


@pytest.mark.parametrize(("lane", "other"), [("backend", "e2e"), ("e2e", "backend")])
@pytest.mark.parametrize("publisher", ["test-results-", "verdict-junit-"])
@pytest.mark.parametrize("job_shape", ["{lane} shard (3/8)", "{lane} full matrix (3/8)"])
def test_partial_reader_never_retires_a_case_from_the_other_lane(
    tmp_path: Path, lane: str, other: str, publisher: str, job_shape: str
) -> None:
    target = replace(entry(), covers_full_suite=False, job=job_shape.format(lane=lane))
    foreign = Bundle(publisher + other + "-shard-3", START.isoformat(), {"case.xml": PASS})
    reader, downloads = source(tmp_path, (foreign,))
    assert reader(target, facts()).outcome == CaseOutcome.UNREADABLE
    assert not downloads
    own = Bundle(publisher + lane + "-shard-3", START.isoformat(), {"case.xml": PASS})
    reader, _ = source(tmp_path, (foreign, own))
    assert reader(target, facts()).outcome == CaseOutcome.PASSED
