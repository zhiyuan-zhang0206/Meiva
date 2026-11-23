from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import cast

import yaml

from scripts.ci import refresh_test_durations as refresh

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _workflow_matrix_groups(workflow_name: str, job_name: str) -> list[int]:
    path = _REPO_ROOT / ".github" / "workflows" / workflow_name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document["jobs"][job_name]["strategy"]["matrix"]["group"]


def _ci_full_static_groups(tmp_path: Path) -> list[int]:
    document = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = document["jobs"]
    assert jobs["backend-static-tests"]["strategy"]["matrix"] == (
        "${{ fromJSON(needs.classify.outputs.static_matrix) }}"
    )
    classify = jobs["classify"]
    assert classify["outputs"]["static_matrix"] == "${{ steps.static_plan.outputs.matrix }}"
    (planner,) = (step["run"] for step in classify["steps"] if step.get("id") == "static_plan")
    output = tmp_path / "outputs"
    ran = subprocess.run(  # noqa: S603 - execute the checked-in FULL planner
        ["bash", "-e", "-c", planner],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "IS_PR_SELECTED": "false",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "2",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs["classify_attempt"] == "2"
    assert outputs["run_id"] == "42"
    return cast("dict[str, list[int]]", json.loads(outputs["matrix"]))["group"]


def test_shard_counts_track_both_workflow_matrices(tmp_path: Path) -> None:
    ci_static = _ci_full_static_groups(tmp_path)
    nightly_static = _workflow_matrix_groups("refresh-test-durations.yml", "measure-static")
    release_static = _workflow_matrix_groups("release-gate.yml", "backend-static")
    ci_backend = _workflow_matrix_groups("ci.yml", "backend-shard")
    nightly_backend = _workflow_matrix_groups("refresh-test-durations.yml", "measure-backend")
    ci_e2e = _workflow_matrix_groups("ci.yml", "e2e-shard")
    nightly_e2e = _workflow_matrix_groups("refresh-test-durations.yml", "measure-e2e")

    assert refresh.STATIC_SHARDS == 8
    assert (
        ci_static == nightly_static == release_static == list(range(1, refresh.STATIC_SHARDS + 1))
    )
    assert refresh.BACKEND_SHARDS == len(ci_backend) == len(nightly_backend)
    assert refresh.E2E_SHARDS == len(ci_e2e) == len(nightly_e2e)


def test_coverage_args_track_the_ci_backend_shard() -> None:
    document = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    expected = sorted(arg.removeprefix("--cov=") for arg in refresh.BACKEND_COVERAGE_ARGS)

    steps = document["jobs"]["backend-shard"]["steps"]
    (command,) = (step["run"] for step in steps if step.get("name") == "Run pytest shard")
    attempts = [
        sorted(re.findall(r"--cov=(\w+)", attempt))
        for attempt in command.split("uv run pytest")[1:]
    ]
    assert len(attempts) == 2
    assert attempts[0] == attempts[1] == expected


def test_coverage_args_track_the_ci_static_shard() -> None:
    document = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    expected = sorted(arg.removeprefix("--cov=") for arg in refresh.BACKEND_COVERAGE_ARGS)
    static_steps = document["jobs"]["backend-static-tests"]["steps"]
    (static_cmd,) = (
        step["run"] for step in static_steps if step.get("name") == "Run static pytest contracts"
    )
    assert sorted(re.findall(r"--cov=(\w+)", static_cmd)) == expected
