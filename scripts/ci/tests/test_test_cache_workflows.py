"""Contract tests keeping `ci.yml` and `cache-warm.yml` test-cache keys and guards aligned."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

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


def test_workflow_switch_and_classify_probe_contract() -> None:
    ci = _load_workflow(_CI_WORKFLOW)
    env = _mapping(ci.get("env"), "ci.yml env")
    assert env["TEST_CACHE_MODE"] == "enforce"

    classify = _jobs(ci, "ci.yml")["classify"]
    outputs = _mapping(classify.get("outputs"), "classify outputs")
    assert outputs["test_cache_skip"] == "${{ steps.test_cache.outputs.skip_enabled }}"
    assert outputs["test_cache_canary"] == "${{ steps.test_cache.outputs.canary }}"

    probe = _step(classify, "Probe PR test cache")
    assert probe.get("id") == "pr_cache"
    assert probe.get("continue-on-error") is True
    probe_with = _mapping(probe.get("with"), "probe with")
    assert probe_with["lookup-only"] is True
    assert probe_with["key"] == f"{_PR_CACHE_PREFIX}none-${{{{ github.run_id }}}}"
    assert probe_with["restore-keys"] == _PR_CACHE_PREFIX
    assert probe_with["path"] == "tmp/test-cache/pr-passes.jsonl"

    decide = _step(classify, "Decide static test cache mode")
    assert decide.get("id") == "test_cache"
    decide_run = _string(decide.get("run"), "decide run")
    assert "scripts/ci/pull_requests/test_cache.py decide" in decide_run
    assert '--mode "$TEST_CACHE_MODE"' in decide_run


def test_dual_prefix_restore_contract() -> None:
    ci_jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    warm_jobs = _jobs(_load_workflow(_WARM_WORKFLOW), "cache-warm.yml")

    plan_main_restore = _mapping(
        _step(ci_jobs["test-cache-plan"], "Restore main recorded passes").get("with"),
        "ci plan main restore",
    )
    assert plan_main_restore["path"] == "tmp/test-cache/passes.jsonl"
    assert plan_main_restore["key"] == f"{_MAIN_CACHE_PREFIX}none-${{{{ github.run_id }}}}"
    assert plan_main_restore["restore-keys"] == _MAIN_CACHE_PREFIX

    plan_pr_restore = _mapping(
        _step(ci_jobs["test-cache-plan"], "Restore PR recorded passes").get("with"),
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
    main_branch = report_run.split('if [[ "${GITHUB_REF}" == "refs/heads/main" ]]; then', 1)[1]
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
    assert "github.ref == 'refs/heads/main'" in _string(save_main.get("if"), "save main if")
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

    ci_plan_image = _string(
        _mapping(ci_jobs["test-cache-plan"].get("container"), "ci plan container").get("image"),
        "ci plan image",
    )
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
    assert ci_plan_image == ci_report_image == ci_static_image == warm_plan_image == warm_seed_image
    assert ci_plan_image.startswith("ghcr.io/zhiyuan-zhang0206/meiva-ci-runner@sha256:")

    ci_plan_run = _string(
        _step(
            ci_jobs["test-cache-plan"],
            "Plan the static-lane tests against the recorded passes",
        ).get("run"),
        "ci plan run",
    )
    ci_report_run = _string(
        _step(ci_jobs["test-cache-report"], "Report cache outcome and record passes").get("run"),
        "ci report run",
    )
    warm_plan_run = _string(
        _step(warm_jobs["test-cache-plan"], "Plan missing static-lane tests").get("run"),
        "warm plan run",
    )
    ci_plan_args = _pytest_arg_flags(ci_plan_run)
    ci_report_args = _pytest_arg_flags(ci_report_run)
    warm_args = _pytest_arg_flags(warm_plan_run)
    assert ci_plan_args, "expected --pytest-arg flags in ci.yml test-cache-plan"
    assert ci_plan_args == ci_report_args == warm_args

    for script in (ci_plan_run, ci_report_run, warm_plan_run):
        env_img = _env_image_flags(script)
        assert len(env_img) == 1
        assert "meiva-ci-runner@sha256:[0-9a-f]*" in env_img[0]


def test_backend_static_fail_open_contract() -> None:
    ci_jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    plan_job = ci_jobs["test-cache-plan"]
    assert "needs.classify.outputs.test_cache_skip == 'true'" in _string(
        plan_job.get("if"), "test-cache-plan if"
    )
    assert plan_job.get("continue-on-error") is True

    static_job = ci_jobs["backend-static-tests"]
    assert static_job["needs"] == ["classify", "test-cache-plan"]
    static_if = _string(static_job.get("if"), "backend-static-tests if")
    assert "needs.test-cache-plan.result == 'success'" in static_if
    assert "needs.test-cache-plan.result == 'skipped'" in static_if
    assert "needs.test-cache-plan.result == 'failure'" in static_if

    static_run = _string(
        _step(static_job, "Run static pytest contracts").get("run"),
        "static run",
    )
    assert (
        'if [[ "$TEST_CACHE_SKIP" == "true" && -s tmp/test-cache/plan.json ]]; then' in static_run
    )
    assert "scripts/ci/pull_requests/test_cache.py shard-targets" in static_run
    assert "::warning title=test cache fail-open::" in static_run


def test_coverage_and_report_job_contract() -> None:
    ci_jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    coverage_step = _step(ci_jobs["backend"], "Combine coverage + gate")
    coverage_if = _string(coverage_step.get("if"), "coverage if")
    assert "needs.classify.outputs.test_cache_skip != 'true'" in coverage_if

    report_job = ci_jobs["test-cache-report"]
    assert "test-cache-report" not in cast("list[str]", ci_jobs["backend"]["needs"])
    assert report_job.get("continue-on-error") is True
    report_if = _string(report_job.get("if"), "report if")
    assert "needs.backend-static-tests.result == 'success'" in report_if
    assert "needs.backend-static-tests.result == 'failure'" in report_if


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
