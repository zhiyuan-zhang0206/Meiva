"""Contract tests keeping `ci.yml` and `cache-warm.yml` test-cache keys and guards aligned."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_WARM_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "cache-warm.yml"
_MAIN_CACHE_PREFIX = "test-pass-main-"
_PR_CACHE_PREFIX = "test-pass-pr-${{ github.event.pull_request.number }}-"


def _mapping(value: object, label: str) -> dict[str, object]:
    assert isinstance(value, dict), f"expected mapping for {label}"
    return cast("dict[str, object]", value)


def _string(value: object, label: str) -> str:
    assert isinstance(value, str), f"expected string for {label}"
    return value


def _load_workflow(path: Path) -> dict[str, object]:
    parsed: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _mapping(parsed, path.name)


def _jobs(workflow: dict[str, object], name: str) -> dict[str, dict[str, object]]:
    raw = _mapping(workflow.get("jobs"), f"{name}.jobs")
    return {job_name: _mapping(job, f"{name}.jobs.{job_name}") for job_name, job in raw.items()}


def _step(job: dict[str, object], name: str) -> dict[str, object]:
    raw_steps = job.get("steps")
    assert isinstance(raw_steps, list), "expected steps list"
    matches: list[dict[str, object]] = []
    for raw_step in cast("list[object]", raw_steps):
        step = _mapping(raw_step, f"step {name!r}")
        if step.get("name") == name:
            matches.append(step)
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def _pytest_arg_flags(script: str) -> list[str]:
    return re.findall(r"--pytest-arg=\S+", script)


def _env_image_flags(script: str) -> list[str]:
    return re.findall(r'--env\s+"image=[^"]+"', script)


def test_dual_prefix_restore_contract() -> None:
    ci_jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    warm_jobs = _jobs(_load_workflow(_WARM_WORKFLOW), "cache-warm.yml")

    plan_main_restore = _mapping(
        _step(ci_jobs["test-cache-report"], "Restore main recorded passes").get("with"),
        "ci plan main restore",
    )
    assert plan_main_restore["path"] == "tmp/test-cache/passes.jsonl"
    assert plan_main_restore["key"] == f"{_MAIN_CACHE_PREFIX}none-${{{{ github.run_id }}}}"
    assert plan_main_restore["restore-keys"] == _MAIN_CACHE_PREFIX

    plan_pr_restore = _mapping(
        _step(ci_jobs["test-cache-report"], "Restore PR recorded passes").get("with"),
        "ci plan pr restore",
    )
    assert plan_pr_restore["path"] == "tmp/test-cache/pr-passes.jsonl"
    assert plan_pr_restore["key"] == f"{_PR_CACHE_PREFIX}none-${{{{ github.run_id }}}}"
    assert plan_pr_restore["restore-keys"] == _PR_CACHE_PREFIX

    warm_restore_with = _mapping(
        _step(warm_jobs["test-cache-plan"], "Restore the recorded passes").get("with"),
        "warm restore with",
    )
    assert warm_restore_with["path"] == "tmp/test-cache/passes.jsonl"
    assert warm_restore_with["key"] == f"{_MAIN_CACHE_PREFIX}none-${{{{ github.run_id }}}}"
    assert warm_restore_with["restore-keys"] == _MAIN_CACHE_PREFIX


def test_report_clears_local_cache_and_isolates_saves() -> None:
    ci_jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    report_run = _string(
        _step(ci_jobs["test-cache-report"], "Report cache outcome and record passes").get("run"),
        "ci report run",
    )
    assert 'rm -f "${git_common_dir}"/meizel-scan-cache-*.sqlite3*' in report_run
    main_branch = report_run.split('elif [[ "${GITHUB_REF}" == "refs/heads/main" ]]; then', 1)[1]
    main_branch = main_branch.split('elif [[ "${GITHUB_EVENT_NAME}" == "pull_request" ]]; then', 1)[
        0
    ]
    assert "test-cache import" not in main_branch
    pr_branch = report_run.split('elif [[ "${GITHUB_EVENT_NAME}" == "pull_request" ]]; then', 1)[1]
    pr_branch = pr_branch.split("else\n", 1)[0]
    assert "test-cache import tmp/test-cache/passes.jsonl" not in pr_branch
    assert "test-cache import tmp/test-cache/pr-passes.jsonl" in pr_branch


def test_save_step_guards_and_keys() -> None:
    ci_jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    warm_jobs = _jobs(_load_workflow(_WARM_WORKFLOW), "cache-warm.yml")

    save_main = _step(ci_jobs["test-cache-report"], "Save main recorded passes")
    save_main_if = _string(save_main.get("if"), "save main if")
    assert "github.ref == 'refs/heads/main'" in save_main_if
    assert "steps.report.outputs.recorded_main == 'true'" in save_main_if
    save_main_with = _mapping(save_main.get("with"), "save main with")
    assert save_main_with["path"] == "tmp/test-cache/passes.jsonl"
    assert save_main_with["key"] == (
        f"{_MAIN_CACHE_PREFIX}${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}"
    )

    save_pr = _step(ci_jobs["test-cache-report"], "Save PR recorded passes")
    save_pr_if = _string(save_pr.get("if"), "save pr if")
    assert "github.event_name == 'pull_request'" in save_pr_if
    assert "steps.report.outputs.recorded_new == 'true'" in save_pr_if
    save_pr_with = _mapping(save_pr.get("with"), "save pr with")
    assert save_pr_with["path"] == "tmp/test-cache/pr-passes.jsonl"
    assert save_pr_with["key"] == (
        f"{_PR_CACHE_PREFIX}${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}"
    )
    assert _MAIN_CACHE_PREFIX not in _string(save_pr_with.get("key"), "save pr key")

    warm_save_with = _mapping(
        _step(warm_jobs["test-cache-save"], "Save the seeded passes").get("with"),
        "warm save with",
    )
    assert warm_save_with["path"] == "tmp/test-cache/passes.jsonl"
    assert warm_save_with["key"] == (
        f"{_MAIN_CACHE_PREFIX}${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}"
    )
    assert warm_jobs["test-cache-plan"]["if"] == "github.ref == 'refs/heads/main'"


def test_warm_and_ci_jobs_share_container_image_and_pytest_args() -> None:
    ci_jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    warm_jobs = _jobs(_load_workflow(_WARM_WORKFLOW), "cache-warm.yml")

    ci_report_image = _string(
        _mapping(ci_jobs["test-cache-report"].get("container"), "ci report container").get("image"),
        "ci report image",
    )
    ci_static_image = _string(
        _mapping(ci_jobs["backend-static-tests"].get("container"), "ci static container").get(
            "image"
        ),
        "ci static image",
    )
    warm_plan_image = _string(
        _mapping(warm_jobs["test-cache-plan"].get("container"), "warm plan container").get("image"),
        "warm plan image",
    )
    warm_seed_image = _string(
        _mapping(warm_jobs["test-cache-seed"].get("container"), "warm seed container").get("image"),
        "warm seed image",
    )
    assert ci_report_image == ci_static_image == warm_plan_image == warm_seed_image
    assert ci_report_image.startswith("ghcr.io/zhiyuan-zhang0206/meiva-ci-runner@sha256:")

    ci_report_run = _string(
        _step(ci_jobs["test-cache-report"], "Report cache outcome and record passes").get("run"),
        "ci report run",
    )
    warm_plan_run = _string(
        _step(warm_jobs["test-cache-plan"], "Plan missing static-lane tests").get("run"),
        "warm plan run",
    )
    ci_report_args = _pytest_arg_flags(ci_report_run)
    warm_args = _pytest_arg_flags(warm_plan_run)
    assert ci_report_args
    assert ci_report_args == warm_args

    for script in (ci_report_run, warm_plan_run):
        env_img = _env_image_flags(script)
        assert len(env_img) == 1
        assert "meiva-ci-runner@sha256:[0-9a-f]*" in env_img[0]


def test_cache_report_runs_after_static_tests_and_outside_backend_gate() -> None:
    ci_jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    assert ci_jobs["backend-static-tests"]["needs"] == ["classify"]
    assert ci_jobs["test-cache-report"]["needs"] == ["classify", "backend-static-tests"]

    report_job = ci_jobs["test-cache-report"]
    assert "test-cache-report" not in cast("list[str]", ci_jobs["backend"]["needs"])
    assert report_job.get("continue-on-error") is True
    report_if = _string(report_job.get("if"), "report if")
    assert "needs.backend-static-tests.result == 'success'" in report_if
    assert "needs.backend-static-tests.result == 'failure'" in report_if


def test_selected_static_matrix_is_published_before_the_artifact() -> None:
    jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    classify = jobs["classify"]
    plan = _step(classify, "Plan static shards")
    upload = _step(classify, "Publish the selected test list")
    steps = cast("list[object]", classify["steps"])
    assert steps.index(plan) < steps.index(upload)
    assert (
        _mapping(classify["outputs"], "outputs")["static_matrix"]
        == "${{ steps.static_plan.outputs.matrix }}"
    )
    assert "continue-on-error" not in plan
    assert "test_cache plan-static" in _string(plan["run"], "planner run")
    assert _mapping(upload["with"], "upload inputs")["path"] == "${{ runner.temp }}/selected-tests/"
    gate = _step(jobs["backend-static-tests"], "Validate static test results")
    gate_inputs = _mapping(gate["with"], "gate inputs")
    assert gate_inputs["min-tests"] == "${{ matrix.min_tests || '1' }}"
    assert gate_inputs["count-skipped"] == (
        "${{ needs.classify.outputs.mode == 'enforce' && "
        "needs.classify.outputs.decision == 'SELECTED' }}"
    )


@pytest.mark.parametrize(
    ("listing", "minimum", "expected_status"),
    [(b"", "0", 0), (b"", "1", 1), (None, "0", 1), (b"corrupt", "0", 1)],
)
def test_static_runner_accepts_only_the_explicit_empty_plan(
    tmp_path: Path, listing: bytes | None, minimum: str, expected_status: int
) -> None:
    jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    static = _step(jobs["backend-static-tests"], "Run static pytest contracts")
    script = _string(static["run"], "static run")
    selected = tmp_path / "tmp" / "selected"
    selected.mkdir(parents=True)
    if listing is not None:
        (selected / "static-1.txt").write_bytes(listing)
    result = subprocess.run(  # noqa: S603 - repository workflow script with fixed test inputs
        [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            script.replace("${{ matrix.group }}", "1").replace("${{ strategy.job-total }}", "8"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "IS_PR_SELECTED": "true",
            "SHARD_DIGEST": hashlib.sha256(b"").hexdigest(),
            "STATIC_MIN_TESTS": minimum,
        },
        check=False,
    )
    assert result.returncode == expected_status, result.stdout + result.stderr
    assert (tmp_path / "tmp" / "junit-backend-static-1.xml").exists() == (expected_status == 0)


def test_warm_workflow_concurrency_and_dep_gate_contract() -> None:
    warm = _load_workflow(_WARM_WORKFLOW)
    concurrency = _mapping(warm.get("concurrency"), "cache-warm concurrency")
    assert concurrency["cancel-in-progress"] is True

    warm_jobs = _jobs(warm, "cache-warm.yml")
    decide_run = _string(
        _step(
            warm_jobs["decide"],
            "Check whether dependency cache inputs changed since last green run",
        ).get("run"),
        "decide run",
    )
    for dep_path in (
        "uv.lock",
        ".python-version",
        ".pre-commit-config.yaml",
        "ui/web/pnpm-lock.yaml",
        "base/cluster/dataplane/runtime_binaries.py",
        ".github/actions/*",
        ".github/workflows/cache-warm.yml",
    ):
        assert dep_path in decide_run

    for job_name in ("python", "node", "native"):
        assert warm_jobs[job_name]["needs"] == ["decide"]
        assert "needs.decide.outputs.warm_deps == 'true'" in _string(
            warm_jobs[job_name].get("if"), f"{job_name} if"
        )


def _shard_reports(tmp_path: Path, *, attempt: int, digests: list[str]) -> None:
    for group, digest in enumerate(digests, 1):
        artifact = tmp_path / f"tmp/static-passes/static-pass-{attempt}-{group}-{digest}"
        artifact.mkdir(parents=True)
        (artifact / f"junit-backend-static-{group}.xml").write_text(
            '<testsuite><testcase name="passes"/></testsuite>'
        )


def _run_workflow_script(
    tmp_path: Path, script: str, **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - reviewed repository workflow shell
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
    )


def test_static_pass_is_published_and_required_after_junit_validation() -> None:
    jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    static_job = jobs["backend-static-tests"]
    publish = _step(static_job, "Publish static shard pass")
    assert publish["if"] == "${{ success() && steps.validate-static.outcome == 'success' }}"
    assert "continue-on-error" not in publish
    steps = cast("list[object]", static_job["steps"])
    assert steps.index(publish) > steps.index(_step(static_job, "Validate static test results"))
    backend = jobs["backend"]
    download = _step(backend, "Download current static shard passes")
    assert _mapping(download["with"], "artifact download")["pattern"] == (
        "static-pass-${{ github.run_attempt }}-*"
    )
    assert "continue-on-error" not in download
    verify = _step(backend, "Verify static shard passes")
    assert "continue-on-error" not in verify and "if" not in verify


@pytest.mark.parametrize("decision", ["FULL", "SELECTED"])
@pytest.mark.parametrize("defect", ["none", "missing", "wrong-plan", "old-attempt"])
def test_backend_demands_every_current_static_pass_even_when_matrix_is_green(
    tmp_path: Path, decision: str, defect: str
) -> None:
    jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    backend = jobs["backend"]
    verify = _step(backend, "Verify static shard passes")

    digests = ["full"] * 8 if decision == "FULL" else ["a" * 64, "b" * 64]
    matrix = (
        {"group": list(range(1, 9))}
        if decision == "FULL"
        else {
            "include": [
                {"group": index, "digest": digest} for index, digest in enumerate(digests, 1)
            ]
        }
    )
    reports = digests.copy()
    if defect == "missing":
        reports.pop()
    if defect == "wrong-plan":
        reports[-1] = "different"
    _shard_reports(tmp_path, attempt=1 if defect == "old-attempt" else 2, digests=reports)
    aggregate = _string(_step(backend, "Verify backend job results")["run"], "aggregate gate")
    aggregate = re.sub(r"\$\{\{ needs\.[\w-]+\.result \}\}", "success", aggregate)
    aggregate = re.sub(r"\$\{\{ needs\.classify\.outputs\.[\w-]+ \}\}", "true", aggregate)
    script = aggregate + "\n" + _string(verify["run"], "static pass gate")
    ran = _run_workflow_script(
        tmp_path,
        script,
        TEST_SELECTION_MODE="enforce",
        DECISION=decision,
        STATIC_MATRIX=json.dumps(matrix),
        GITHUB_RUN_ATTEMPT="2",
    )
    assert "backend jobs verified" in ran.stdout
    assert ran.returncode == (0 if defect == "none" else 1), ran.stdout + ran.stderr
    if defect != "none":
        assert "Missing static shard pass:" in ran.stdout


@pytest.mark.parametrize("complete", [False, True])
def test_full_cache_recording_requires_all_static_reports(tmp_path: Path, complete: bool) -> None:
    jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    job = jobs["test-cache-report"]
    verify = _string(_step(job, "Verify static shard passes")["run"], "report pass gate")
    assert verify == _step(jobs["backend"], "Verify static shard passes")["run"]
    _shard_reports(tmp_path, attempt=1, digests=["full"] * (8 if complete else 1))
    checked = _run_workflow_script(
        tmp_path,
        verify,
        STATIC_MATRIX=json.dumps({"group": list(range(1, 9))}),
        GITHUB_RUN_ATTEMPT="1",
    )
    assert (checked.returncode == 0) == complete, checked.stdout + checked.stderr

    outputs = tmp_path / "outputs"
    git_dir = tmp_path / "fake-git"
    git_dir.mkdir()
    restore = tmp_path / "tmp/test-cache/passes.jsonl"
    restore.parent.mkdir(parents=True)
    restore.write_text("restored pass\n")
    fake_tools = """
    uv() {
      case "$*" in
        *"test-cache plan"*) echo '{}' ;;
        *"test-cache report"*) echo '{"would_skip_failed":[]}' ;;
        *"test-cache record"*) echo '{"recorded":1}'; touch recorded ;;
        *"test-cache export"*) echo 'fresh pass' ;;
      esac
    }
    git() { printf '%s\n' "$FAKE_GIT_DIR"; }
    """
    report = _string(_step(job, "Report cache outcome and record passes")["run"], "report shell")
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("image: meiva-ci-runner@sha256:deadbeef\n")
    ran = _run_workflow_script(
        tmp_path,
        fake_tools + report,
        GITHUB_REF="refs/heads/main",
        GITHUB_EVENT_NAME="schedule",
        DECISION="FULL",
        SELECTION_MODE="enforce",
        RUN_ID="1",
        GITHUB_OUTPUT=str(outputs),
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary"),
        FAKE_GIT_DIR=str(git_dir),
        STATIC_PASSES="success" if complete else "failure",
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert (tmp_path / "recorded").exists() == complete
    assert restore.read_text() == ("fresh pass\n" if complete else "restored pass\n")
    assert outputs.exists() == complete
    if complete:
        assert "recorded_main=true" in outputs.read_text()
