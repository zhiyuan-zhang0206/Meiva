from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from scripts.audit.analysis import app_provided_checks, expected_checks

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_expected_checks_match_ci_workflow_aggregator_job_names() -> None:
    ci_path = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
    document = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = cast("dict[str, dict[str, Any]]", document["jobs"])
    job_names = {str(job["name"]) for job in jobs.values() if "name" in job}
    missing = expected_checks() - job_names - app_provided_checks()
    assert not missing, (
        f"expected_checks() missing from .github/workflows/ci.yml: {sorted(missing)}"
    )


# meiva:note
# An exemption that is not itself required, or that overlaps the job names, would either hide a
# genuinely missing job or quietly excuse a name that ci.yml does provide. Both are silent.
def test_app_provided_checks_are_required_and_absent_from_the_workflow() -> None:
    ci_path = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
    document = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = cast("dict[str, dict[str, Any]]", document["jobs"])
    job_names = {str(job["name"]) for job in jobs.values() if "name" in job}
    assert app_provided_checks() <= expected_checks()
    assert not app_provided_checks() & job_names


def test_trunk_merge_queue_configuration_is_retired() -> None:
    assert not (_REPO_ROOT / ".trunk" / "trunk.yaml").exists()


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
