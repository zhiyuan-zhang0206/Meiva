from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from scripts.audit.analysis import app_provided_checks, expected_checks

_REPO_ROOT = Path(__file__).resolve().parents[2]


# meiva:note
# What makes the declaration consistent with the workflow that must carry it: every required check is
# either a job in `ci.yml` or an app-provided check, a job the workflow does carry must not also be
# exempted, and an exemption must describe a check that is actually required. Each mistake is silent
# on its own — a missing job never runs, a shadowed exemption excuses a job that does exist, and a
# stray exemption is a licence waiting to be used. The predicate lives here because it guards a
# declaration against a workflow; nothing at runtime asks it anything.
def _contract_problems(
    expected: frozenset[str], app_provided: frozenset[str], job_names: frozenset[str]
) -> list[str]:
    problems: list[str] = []
    shadowed = sorted(app_provided & job_names)
    if shadowed:
        problems.append(f"app-provided checks that are also workflow jobs: {', '.join(shadowed)}")
    missing = sorted(expected - job_names - app_provided)
    if missing:
        problems.append(f"required checks missing from the workflow: {', '.join(missing)}")
    strayed = sorted(app_provided - expected)
    if strayed:
        problems.append(f"app-provided checks that are not required: {', '.join(strayed)}")
    return problems


def _job_names() -> frozenset[str]:
    ci_path = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
    document = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = cast("dict[str, dict[str, Any]]", document["jobs"])
    return frozenset(str(job["name"]) for job in jobs.values() if "name" in job)


def test_the_contract_names_only_workflow_jobs_and_declared_app_checks() -> None:
    assert _contract_problems(expected_checks(), app_provided_checks(), _job_names()) == []


# meiva:note
# Each of these is a mutation of the real contract: if any of them stops reported, the exemption
# mechanism has become a licence to name a check that nothing carries.
def test_a_required_check_that_no_job_carries_is_a_problem() -> None:
    problems = _contract_problems(frozenset({"a", "b"}), frozenset(), frozenset({"a"}))
    assert problems == ["required checks missing from the workflow: b"]


def test_an_exemption_for_a_real_job_name_is_a_problem() -> None:
    problems = _contract_problems(
        frozenset({"classify change"}),
        frozenset({"classify change"}),
        frozenset({"classify change"}),
    )
    assert problems == ["app-provided checks that are also workflow jobs: classify change"]


def test_an_exemption_that_is_not_required_is_a_problem() -> None:
    problems = _contract_problems(frozenset({"a"}), frozenset({"spare"}), frozenset({"a"}))
    assert problems == ["app-provided checks that are not required: spare"]


# meiva:note
# A path-filtered suite reports SKIPPED on the pull requests it does not apply to, so its own name can
# never be a required check. These two gates are the always-reporting face of `frontend (eslint + tsc
# + vitest)` and `e2e (Playwright happy path)`: they run unconditionally, pass when the suite passed or
# was skipped, and fail when it failed. The assertions below pin the three properties that make that
# true — it runs on every pull request, it looks at the suite it fronts, and it fails closed when
# classification did not succeed.
def test_always_reporting_gates_front_the_path_filtered_suites() -> None:
    ci_path = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
    document = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = cast("dict[str, dict[str, Any]]", document["jobs"])

    for gate, suite in (("frontend-gate", "frontend"), ("e2e-gate", "e2e")):
        job = jobs[gate]
        assert job["name"] == gate.replace("-", " ")
        assert job["if"] == "${{ always() }}"
        assert set(cast("list[str]", job["needs"])) == {"classify", suite}
        steps = cast("list[dict[str, Any]]", job["steps"])
        env = cast("dict[str, str]", steps[0]["env"])
        assert env["CLASSIFY"] == "${{ needs.classify.result }}"
        assert env["SUITE"] == "${{ needs." + suite + ".result }}"
