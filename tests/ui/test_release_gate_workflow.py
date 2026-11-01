from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RELEASE_GATE = _REPO_ROOT / ".github" / "workflows" / "release-gate.yml"
_PROMOTE_RELEASE = _REPO_ROOT / ".github" / "workflows" / "promote-release.yml"


def _yaml(path: Path) -> dict[object, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[object, Any]", document)


def _step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def test_release_gate_supports_scheduled_train_and_manual_dispatch() -> None:
    workflow = _yaml(_RELEASE_GATE)
    triggers = workflow[True]

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "17 */6 * * *"}]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"target_ref", "version", "hotfix_of", "reason"}
    assert inputs["version"]["required"] is False
    assert "pull-requests" not in workflow["permissions"]


def test_every_leg_runs_on_the_commit_the_precheck_resolved_and_skips_idle_train() -> None:
    jobs = _yaml(_RELEASE_GATE)["jobs"]

    assert set(jobs) == {"precheck", "backend", "e2e", "platform-smoke", "certify"}
    for name in ("backend", "e2e", "platform-smoke"):
        assert jobs[name]["needs"] == "precheck"
        assert jobs[name]["if"] == "needs.precheck.outputs.skip_train != 'true'"
        checkout = jobs[name]["steps"][0]
        assert checkout["with"]["ref"] == "${{ needs.precheck.outputs.sha }}"


def test_precheck_validates_target_base_and_nightly_verdict() -> None:
    precheck = _yaml(_RELEASE_GATE)["jobs"]["precheck"]
    resolve = _step(precheck["steps"], "Resolve target and check idle train")
    refusal = _step(precheck["steps"], "Refuse a tree without a certified base")
    nightly = _step(precheck["steps"], "Refuse a commit whose nightly full suite was red")

    assert "scripts/host_ops/release/certify.py precheck" in resolve["run"]
    assert "scripts/host_ops/release/label.py" not in _RELEASE_GATE.read_text(encoding="utf-8")
    assert "scripts/host_ops/release/base.py" in refusal["run"]
    assert "--bootstrap-when-empty" in refusal["run"]
    assert "refresh-test-durations.yml" in nightly["run"]
    assert 'failure) echo "::error::' in nightly["run"]


def test_certify_mints_git_native_certificate_without_reverse_pr() -> None:
    jobs = _yaml(_RELEASE_GATE)["jobs"]
    certify = jobs["certify"]
    assert certify["needs"] == ["precheck", "backend", "e2e", "platform-smoke"]
    assert "skip_train != 'true'" in certify["if"]

    mint = _step(certify["steps"], "Mint annotated release certificate")
    publish = _step(certify["steps"], "Publish release tag, channel/latest, and GitHub Release")
    assert "scripts/host_ops/release/certify.py mint" in mint["run"]
    assert "refs/tags/channel/latest" in publish["run"]
    assert "gh release create" in publish["run"]
    assert "gh pr create" not in _RELEASE_GATE.read_text(encoding="utf-8")


def test_promote_workflow_verifies_certificate_and_updates_channel_stable() -> None:
    workflow = _yaml(_PROMOTE_RELEASE)
    triggers = workflow[True]
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["version"]["required"] is True

    promote_job = workflow["jobs"]["promote"]
    verify_step = _step(
        promote_job["steps"], "Verify release certificate and update channel/stable"
    )
    push_step = _step(promote_job["steps"], "Push channel/stable and mark GitHub Release latest")
    assert "scripts/host_ops/release/promote.py" in verify_step["run"]
    assert "refs/tags/channel/stable:refs/tags/channel/stable" in push_step["run"]
    assert "--latest" in push_step["run"]
