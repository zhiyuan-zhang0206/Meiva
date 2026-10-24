# meiva:link scripts/ci/docs/file-shard-shadow/required-ci.meiva.okf.md

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.ci.pull_requests.shared_plan_input import (
    PlanRun,
    artifact_id,
    producer_attempt,
    records,
)

RUN = PlanRun(
    "o/r",
    40114717531,
    2,
    1,
    "88a8e744e6ffdd863c3b889d975031804c995059",
    "backend shared collection plan",
)


# meiva:note
# Keep the native job fields from run 40114717531 and add the new producer generation step.
def _job(attempt: int = 1, **changes: Any) -> dict[str, Any]:
    return {
        "id": 120933519351 + attempt,
        "run_id": RUN.run_id,
        "workflow_name": "CI",
        "head_branch": "trunk-merge/pr-6957/fca509c9-56d0-4d24-8ceb-3d90b65e10d2",
        "run_attempt": attempt,
        "head_sha": RUN.source_sha,
        "status": "completed",
        "conclusion": "success",
        "name": RUN.producer,
        "steps": [
            {
                "name": f"Shared plan generation {attempt}",
                "status": "completed",
                "conclusion": "success",
            }
        ],
        "labels": ["ubuntu-24.04"],
        "runner_id": 1000468490,
    } | changes


def _artifact(attempt: int = 1, **changes: Any) -> dict[str, Any]:
    return {
        "id": 97,
        "name": f"backend-file-plan-{RUN.run_id}-{attempt}",
        "expired": False,
        "workflow_run": {"id": RUN.run_id, "head_sha": RUN.source_sha},
    } | changes


def test_copied_forward_success_preserves_its_executed_generation() -> None:
    original = _job()
    copied = _job(2, id=120903303393, steps=original["steps"])
    assert producer_attempt([original, copied], RUN) == 1
    assert producer_attempt([original, copied], replace(RUN, floor=2)) is None
    assert producer_attempt([copied], RUN) == 1


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [{"name": "Shared plan generation 1"}],
        [{"name": "Shared plan generation 01", "status": "completed", "conclusion": "success"}],
        [{"name": "Shared plan generation 3", "status": "completed", "conclusion": "success"}],
        [{"name": "Shared plan generation 1", "status": "completed", "conclusion": "failure"}],
        [{"name": "Shared plan generation 1", "status": "completed", "conclusion": "success"}] * 2,
    ],
)
def test_generation_marker_is_unique_strict_and_successful(steps: list[dict[str, Any]]) -> None:
    with pytest.raises((ValueError, KeyError)):
        producer_attempt([_job(2, steps=steps)], RUN)


def test_generation_floor_distinguishes_full_and_failed_only_reruns() -> None:
    old = [_job()]
    assert producer_attempt(old, RUN) == 1
    assert producer_attempt(old, replace(RUN, floor=2)) is None
    assert producer_attempt([], RUN) is None
    assert producer_attempt([_job(conclusion="failure")], RUN) is None
    assert producer_attempt([*old, _job(2, status="queued", conclusion=None)], RUN) is None
    assert producer_attempt([*old, _job(2)], RUN) == 2
    with pytest.raises(ValueError, match="ended failure"):
        producer_attempt([*old, _job(2, conclusion="failure")], RUN)
    with pytest.raises(ValueError, match="ended failure"):
        producer_attempt([_job(conclusion="failure")], replace(RUN, attempt=1))


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending", "requested"])
def test_newest_registered_producer_blocks_old_success(status: str) -> None:
    assert producer_attempt([_job(), _job(2, status=status, conclusion=None)], RUN) is None


@pytest.mark.parametrize(
    "conclusion",
    [
        "failure",
        "cancelled",
        "timed_out",
        "skipped",
        "neutral",
        "action_required",
        "stale",
        "startup_failure",
    ],
)
def test_current_failed_producer_never_reuses_prior_success(conclusion: str) -> None:
    with pytest.raises(ValueError, match="Shared plan producer ended"):
        producer_attempt([_job(), _job(2, conclusion=conclusion)], RUN)


@pytest.mark.parametrize(
    "changes",
    [
        {"run_attempt": True},
        {"run_attempt": 0},
        {"run_attempt": 3},
        {"run_id": True},
        {"run_id": 99},
        {"head_sha": "merge-sha"},
        {"status": "unknown"},
        {"conclusion": "unknown"},
        {"status": "queued"},
    ],
)
def test_malformed_or_foreign_producer_fails_closed(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        producer_attempt([_job(**changes)], RUN)


def test_duplicate_producer_and_missing_required_metadata_fail_closed() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        producer_attempt([_job(), _job()], RUN)
    job = _job()
    del job["run_attempt"]
    with pytest.raises(KeyError):
        producer_attempt([job], RUN)


def test_complete_pagination_considers_the_highest_actual_attempt() -> None:
    pages = [{"total_count": 2, "jobs": [_job()]}, {"total_count": 2, "jobs": [_job(2)]}]
    assert producer_attempt(records(pages, "jobs"), RUN) == 2
    with pytest.raises(ValueError, match="complete population"):
        records(pages[:1], "jobs")


@pytest.mark.parametrize("identifier", [None, True, 0, -1, "9", {}])
def test_pagination_requires_native_positive_record_ids(identifier: object) -> None:
    with pytest.raises(ValueError, match="positive native ids"):
        records([{"total_count": 1, "jobs": [_job(id=identifier)]}], "jobs")


def test_repeated_unrelated_row_cannot_hide_a_newer_producer() -> None:
    unrelated = _job(id=10, name="another job")
    pages = [
        {"total_count": 3, "jobs": [_job(), unrelated]},
        {"total_count": 3, "jobs": [unrelated]},
    ]
    with pytest.raises(ValueError, match="repeated a native record"):
        records(pages, "jobs")


@pytest.mark.parametrize(
    "pages",
    [None, [], [{}], [{"total_count": True, "jobs": []}], [{"total_count": 1, "jobs": [None]}]],
)
def test_malformed_pagination_fails_closed(pages: object) -> None:
    with pytest.raises((ValueError, KeyError)):
        records(pages, "jobs")


def test_only_successful_producers_exact_artifact_is_eligible() -> None:
    assert artifact_id([_artifact()], RUN, 1) == 97
    assert artifact_id([_artifact()], RUN, 2) is None
    assert artifact_id([], RUN, 1) is None
    with pytest.raises(ValueError, match="ambiguous"):
        artifact_id([_artifact(), _artifact()], RUN, 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": True},
        {"id": 0},
        {"expired": True},
        {"expired": None},
        {"workflow_run": {"id": True, "head_sha": RUN.source_sha}},
        {"workflow_run": {"id": 99, "head_sha": RUN.source_sha}},
        {"workflow_run": {"id": RUN.run_id, "head_sha": "merge-sha"}},
    ],
)
def test_foreign_expired_or_malformed_artifact_fails_closed(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        artifact_id([_artifact(**changes)], RUN, 1)


@pytest.mark.parametrize("scenario", ["success", "timeout", "error", "malformed", "floor", "nan"])
# meiva:note
# A real CLI process invokes a test-owned gh executable; no application owner is patched.
def test_cli_obeys_bounded_transport_and_writes_only_verified_outputs(
    tmp_path: Path, scenario: str
) -> None:
    binary = tmp_path / "gh"
    jobs = [{"total_count": 1, "jobs": [_job()]}]
    artifacts = [{"total_count": 1, "artifacts": [_artifact()]}]
    binary.write_text(
        f"#!{sys.executable}\nimport sys,time,json\n"
        f"scenario={scenario!r}\n"
        "if scenario == 'timeout': time.sleep(10)\n"
        "if scenario == 'error': sys.exit(17)\n"
        "if scenario == 'malformed': print('{}'); sys.exit(0)\n"
        f"print(json.dumps({jobs!r} if '/jobs?' in sys.argv[-1] else {artifacts!r}))\n"
    )
    binary.chmod(0o755)
    output, record = tmp_path / "outputs", tmp_path / "producer.json"
    root = Path(__file__).resolve().parents[4]
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "scripts.ci.pull_requests.shared_plan_input",
            "--repo",
            RUN.repo,
            "--run-id",
            str(RUN.run_id),
            "--attempt",
            "2",
            "--floor",
            "2" if scenario == "floor" else "1",
            "--source-sha",
            RUN.source_sha,
            "--producer",
            RUN.producer,
            "--output",
            str(output),
            "--record",
            str(record),
            "--timeout",
            "nan" if scenario == "nan" else "0.5" if scenario in {"floor", "timeout"} else "5",
        ],
        cwd=root,
        env=os.environ | {"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
        timeout=8,
    )
    assert result.returncode == (0 if scenario == "success" else 1), result.stderr
    if scenario == "success":
        assert output.read_text() == "artifact_id=97\nproducer_attempt=1\n"
        assert json.loads(record.read_text())["producer_attempt"] == 1
    else:
        assert not output.exists() and not record.exists()
        assert "Shared plan input failed:" in result.stderr
