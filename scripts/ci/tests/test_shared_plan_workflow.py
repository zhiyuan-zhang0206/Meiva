# meiva:link scripts/ci/docs/file-shard-shadow/required-ci.meiva.okf.md

import os
import subprocess
from pathlib import Path

import pytest
import yaml


def test_full_producer_and_shards_prepare_concurrently_after_classification() -> None:
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]
    plan, shard = jobs["backend-plan"], jobs["backend-shard"]
    assert plan["needs"] == shard["needs"] == ["classify"]
    assert plan["if"] == shard["if"]
    assert "!cancelled()" in plan["if"] and "SELECTED" in plan["if"]
    classify = jobs["classify"]
    assert classify["outputs"]["attempt"] == "${{ steps.generation.outputs.attempt }}"
    generation = next(step for step in classify["steps"] if step.get("id") == "generation")
    assert '"$GITHUB_RUN_ATTEMPT"' in generation["run"]
    assert jobs["backend-selected"]["needs"] == ["classify"]


def test_consumers_require_the_producers_exact_artifact_and_duration_input() -> None:
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]
    shard = jobs["backend-shard"]
    steps = {step.get("name"): step for step in shard["steps"]}
    wait = steps["Await the successful shared plan producer"]
    assert wait["env"]["CLASSIFY_ATTEMPT"] == "${{ needs.classify.outputs.attempt }}"
    assert wait["env"]["SOURCE_SHA"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    assert '--floor "$CLASSIFY_ATTEMPT"' in wait["run"]
    download = steps["Download this run's shared plan"]
    assert download["with"]["artifact-ids"] == "${{ steps.shared-plan.outputs.artifact_id }}"
    assert download.get("continue-on-error", False) is False
    assert "restore-test-durations" not in str(shard)
    run = steps["Run pytest shard"]["run"]
    assert "plan_file_shards" not in run
    assert run.count("--file-shard-shared --file-shard-count=8") == 2


@pytest.mark.parametrize(
    ("duration", "generation"), [(None, "2"), ("{}", "2"), ('{"a::test_a": 0.5}', "2"), ("{}", "1")]
)
# meiva:note
# These are the checked-in producer/consumer shell steps over test-owned files.
def test_paired_durations_restore_exact_bytes_or_absence(
    tmp_path: Path, duration: str | None, generation: str
) -> None:
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]
    pair = next(
        step["run"]
        for step in jobs["backend-plan"]["steps"]
        if step.get("name") == "Pair the plan with its exact duration input"
    )
    restore = next(
        step["run"]
        for step in jobs["backend-shard"]["steps"]
        if step.get("name") == "Restore the plan's paired duration input"
    )
    (tmp_path / "tmp/file-shards").mkdir(parents=True)
    target = tmp_path / ".test_durations"
    if duration is not None:
        target.write_text(duration)
    subprocess.run(  # noqa: S603
        ["bash", "-euc", pair],
        cwd=tmp_path,
        env=os.environ | {"GITHUB_RUN_ATTEMPT": "2"},
        check=True,
        timeout=5,
    )
    target.write_text('{"wrong-cache": 999}')
    result = subprocess.run(  # noqa: S603
        ["bash", "-euc", restore],
        cwd=tmp_path,
        env=os.environ | {"PRODUCER_ATTEMPT": generation},
        check=False,
        timeout=5,
    )
    assert result.returncode == (0 if generation == "2" else 1)
    if generation != "2":
        assert target.read_text() == '{"wrong-cache": 999}'
    else:
        assert target.read_text() == duration if duration is not None else not target.exists()


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
