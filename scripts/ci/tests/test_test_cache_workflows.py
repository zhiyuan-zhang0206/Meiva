# meiva:note
# Contract tests keeping `ci.yml` and `cache-warm.yml` test-cache keys and guards aligned.

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_WARM_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "cache-warm.yml"
_CACHE_PREFIX = "test-pass-main-"


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


def test_test_cache_prefix_and_main_only_save_contract() -> None:
    ci_jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    warm_jobs = _jobs(_load_workflow(_WARM_WORKFLOW), "cache-warm.yml")

    ci_restore_with = _mapping(
        _step(ci_jobs["test-cache-plan"], "Restore the recorded passes").get("with"),
        "ci restore with",
    )
    assert ci_restore_with["key"] == f"{_CACHE_PREFIX}none-${{{{ github.run_id }}}}"
    assert ci_restore_with["restore-keys"] == _CACHE_PREFIX

    warm_restore_with = _mapping(
        _step(warm_jobs["test-cache-plan"], "Restore the recorded passes").get("with"),
        "warm restore with",
    )
    assert warm_restore_with["key"] == f"{_CACHE_PREFIX}none-${{{{ github.run_id }}}}"
    assert warm_restore_with["restore-keys"] == _CACHE_PREFIX

    ci_report_run = _string(
        _step(
            ci_jobs["test-cache-report"],
            "Report what the cache would have skipped, and record the passes",
        ).get("run"),
        "ci report run",
    )
    assert 'if [[ "${GITHUB_REF}" == "refs/heads/main" ]]; then' in ci_report_run
    assert "test-cache import" not in ci_report_run

    ci_save = _step(ci_jobs["test-cache-report"], "Save the recorded passes")
    assert "github.ref == 'refs/heads/main'" in _string(ci_save.get("if"), "ci save if")
    ci_save_with = _mapping(ci_save.get("with"), "ci save with")
    assert ci_save_with["key"] == (
        f"{_CACHE_PREFIX}${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}"
    )

    warm_save_with = _mapping(
        _step(warm_jobs["test-cache-save"], "Save the seeded passes").get("with"),
        "warm save with",
    )
    assert warm_save_with["key"] == (
        f"{_CACHE_PREFIX}${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}"
    )
    assert warm_jobs["test-cache-plan"]["if"] == "github.ref == 'refs/heads/main'"


def test_warm_and_ci_plan_share_container_image_and_pytest_args() -> None:
    ci_jobs = _jobs(_load_workflow(_CI_WORKFLOW), "ci.yml")
    warm_jobs = _jobs(_load_workflow(_WARM_WORKFLOW), "cache-warm.yml")

    ci_plan_image = _string(
        _mapping(ci_jobs["test-cache-plan"].get("container"), "ci plan container").get("image"),
        "ci plan image",
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
    assert ci_plan_image == ci_static_image == warm_plan_image == warm_seed_image
    assert ci_plan_image.startswith("ghcr.io/zhiyuan-zhang0206/meiva-ci-runner@sha256:")

    ci_plan_run = _string(
        _step(
            ci_jobs["test-cache-plan"],
            "Plan the static-lane tests against the recorded passes",
        ).get("run"),
        "ci plan run",
    )
    warm_plan_run = _string(
        _step(warm_jobs["test-cache-plan"], "Plan missing static-lane tests").get("run"),
        "warm plan run",
    )
    ci_args = _pytest_arg_flags(ci_plan_run)
    warm_args = _pytest_arg_flags(warm_plan_run)
    assert ci_args, "expected --pytest-arg flags in ci.yml test-cache-plan"
    assert ci_args == warm_args

    ci_env_img = _env_image_flags(ci_plan_run)
    warm_env_img = _env_image_flags(warm_plan_run)
    assert len(ci_env_img) == 1 and len(warm_env_img) == 1
    assert "meiva-ci-runner@sha256:[0-9a-f]*" in ci_env_img[0]
    assert "meiva-ci-runner@sha256:[0-9a-f]*" in warm_env_img[0]


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
