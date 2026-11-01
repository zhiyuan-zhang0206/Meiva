from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from scripts.audit.analysis import expected_checks

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_expected_checks_match_ci_workflow_aggregator_job_names() -> None:
    ci_path = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
    document = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = cast("dict[str, dict[str, Any]]", document["jobs"])
    job_names = {str(job["name"]) for job in jobs.values() if "name" in job}
    missing = expected_checks() - job_names
    assert not missing, (
        f"expected_checks() missing from .github/workflows/ci.yml: {sorted(missing)}"
    )


def test_trunk_merge_queue_configuration_is_retired() -> None:
    assert not (_REPO_ROOT / ".trunk" / "trunk.yaml").exists()
