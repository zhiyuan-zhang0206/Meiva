# meiva:link scripts/ci/docs/file-shard-shadow/required-ci.meiva.okf.md

import os
import subprocess
from pathlib import Path

import pytest
import yaml


def test_only_known_full_routes_plan_concurrently() -> None:
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]
    plan = jobs["backend-plan"]
    shard = jobs["backend-shard"]
    assert "needs" not in plan
    assert "backend-plan" in shard["needs"]
    assert "workflow_dispatch" in plan["if"]
    assert "trunk-merge/" in plan["if"] and "trunk-temp/" in plan["if"]
    assert "mode" not in plan["if"] and "SELECTED" not in plan["if"]
    assert jobs["backend-selected"]["needs"] == ["classify"]


def test_consumers_require_the_producers_exact_artifact_and_duration_input() -> None:
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]
    shard = jobs["backend-shard"]
    steps = {step.get("name"): step for step in shard["steps"]}
    download = steps["Download this run's shared plan"]
    assert download["with"]["artifact-ids"] == "${{ needs.backend-plan.outputs.artifact_id }}"
    assert download.get("continue-on-error", False) is False
    restore = steps["Restore the shared plan's test durations"]
    assert restore["with"]["key"] == "${{ needs.backend-plan.outputs.durations_key }}"
    run = steps["Run pytest shard"]["run"]
    assert 'if [ "$SHARED_FILE_PLAN" = true ]; then' in run
    assert "shared=(--file-shard-shared --file-shard-count=8)" in run
    assert run.count('"${shared[@]}"') == 2
    aggregate = jobs["backend"]
    assert "backend-plan" in aggregate["needs"]
    verdict = next(
        step for step in aggregate["steps"] if step.get("name") == "Verify backend job results"
    )
    assert 'check backend-plan "${{ needs.backend-plan.result }}"' in verdict["run"]
    assert 'if [ -z "$PLAN_ARTIFACT_ID" ]; then fail' in verdict["run"]


def test_required_plan_transport_retries_once_without_replanning() -> None:
    root = Path(__file__).resolve().parents[3]
    job = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]["backend-plan"]
    steps = {step.get("id"): step for step in job["steps"]}
    first, retry = steps["publish"], steps["publish-retry"]
    assert retry["if"] == "${{ steps.publish.outcome == 'failure' }}"
    assert first["with"]["name"] == retry["with"]["name"]
    assert first["with"]["path"] == retry["with"]["path"]
    assert retry["with"]["overwrite"] is True
    assert job["outputs"]["artifact_id"] == "${{ steps.required-plan.outputs.artifact_id }}"
    required = steps["required-plan"]
    assert "steps.publish.outcome == 'success'" in required["env"]["PLAN_ARTIFACT_ID"]
    assert "steps.publish-retry.outcome == 'success'" in required["env"]["PLAN_ARTIFACT_ID"]
    assert "steps.publish-retry.outputs.artifact-id" in required["env"]["PLAN_ARTIFACT_ID"]
    assert 'test -n "$PLAN_ARTIFACT_ID"' in required["run"]


@pytest.mark.parametrize(
    ("outcome", "artifact", "expected"),
    [
        ("success", "123", 0),
        ("success", "", 1),
        ("failure", "123", 1),
        ("skipped", "", 1),
        ("cancelled", "", 1),
    ],
)
# meiva:note
# The checked-in gate runs over test-owned dependency results, without launching CI tooling.
def test_shared_plan_failure_cannot_be_hidden_by_green_shards(
    outcome: str, artifact: str, expected: int
) -> None:
    root = Path(__file__).resolve().parents[3]
    job = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]["backend"]
    script = next(
        step["run"] for step in job["steps"] if step.get("name") == "Verify backend job results"
    )
    script = script.replace("${{ needs.classify.outputs.backend }}", "true")
    script = script.replace("${{ needs.classify.outputs.selector }}", "success")
    for name in job["needs"]:
        script = script.replace(
            "${{ needs." + name + ".result }}", outcome if name == "backend-plan" else "success"
        )
    assert "${{" not in script
    result = subprocess.run(  # noqa: S603
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        env={
            **os.environ,
            "SHARED_FILE_PLAN": "true",
            "PLAN_ARTIFACT_ID": artifact,
            "TEST_SELECTION_MODE": "enforce",
            "DECISION": "FULL",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == expected, result.stdout + result.stderr
