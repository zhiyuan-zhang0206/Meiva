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

    assert set(triggers) == {"schedule", "workflow_dispatch", "pull_request"}
    assert triggers["schedule"] == [{"cron": "17 */6 * * *"}]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"verify_only", "target_ref", "version", "hotfix_of", "reason"}
    assert inputs["verify_only"] == {
        "description": "Verify this workflow's commit without publishing (main preflight)",
        "type": "boolean",
        "default": False,
    }
    assert inputs["version"]["required"] is False
    assert "pull-requests" not in workflow["permissions"]


_CHECKS = ("static-and-structure", "backend-static", "frontend", "backend", "e2e", "platform-smoke")


def test_preflight_checks_its_dispatch_commit_and_cannot_publish() -> None:
    workflow = _yaml(_RELEASE_GATE)
    jobs = workflow["jobs"]
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert "Verify release tree {0}" in workflow["run-name"]
    assert "inputs.verify_only == true" in workflow["run-name"]
    assert "verify-release-{0}" in workflow["concurrency"]["group"]
    resolve = _step(jobs["precheck"]["steps"], "Resolve target and check idle train")
    assert '"$VERIFY_ONLY" = true' in resolve["run"]
    assert '"$EVENT_NAME" = pull_request' in resolve["run"]
    assert '"$GITHUB_SHA" >> "$GITHUB_OUTPUT"' in resolve["run"]
    assert "github.event_name != 'pull_request'" in jobs["certify"]["if"]
    assert "inputs.verify_only != true" in jobs["certify"]["if"]
    lookup = _step(jobs["precheck"]["steps"], "Find a complete verification for this tree")
    assert "steps.target.outputs.source_ref == 'refs/heads/main'" in lookup["if"]


def test_every_leg_runs_on_the_commit_the_precheck_resolved_and_skips_idle_train() -> None:
    jobs = _yaml(_RELEASE_GATE)["jobs"]
    assert set(jobs) == {
        "precheck",
        "certify",
        "release-app",
        "release-android",
        "publish-client-release",
        *_CHECKS,
    }
    for name in _CHECKS:
        job = jobs[name]
        assert job["needs"] == "precheck"
        assert job["if"] == (
            "needs.precheck.outputs.skip_train != 'true' && needs.precheck.outputs.evidence_run == ''"
        )
        checkout = job["steps"][0]
        assert checkout["with"]["ref"] == "${{ needs.precheck.outputs.sha }}"
        assert checkout["with"]["persist-credentials"] is False
        assert job["container"]["image"].startswith(
            "ghcr.io/zhiyuan-zhang0206/meiva-ci-runner@sha256:"
        )
        assert job["defaults"]["run"]["shell"] == "bash"
        assert job["container"]["options"] == "--init --shm-size=2g"


def test_release_gate_splits_restore_durations_resolved_by_precheck() -> None:
    jobs = _yaml(_RELEASE_GATE)["jobs"]
    assert jobs["precheck"]["outputs"]["durations_key"] == "${{ steps.durations.outputs.key }}"
    resolve = _step(jobs["precheck"]["steps"], "Resolve the test durations")
    assert resolve["uses"] == "./.github/actions/resolve-test-durations"
    assert resolve["if"] == (
        "steps.target.outputs.skip_train != 'true' && steps.evidence.outputs.run_id == ''"
    )
    assert jobs["backend-static"]["strategy"]["matrix"]["group"] == list(range(1, 9))
    for split_leg in ("backend-static", "backend", "e2e"):
        restore = _step(jobs[split_leg]["steps"], "Restore the test durations")
        assert restore["uses"] == "./.github/actions/restore-test-durations"
        assert restore["with"]["key"] == "${{ needs.precheck.outputs.durations_key }}"


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
    assert certify["needs"] == ["precheck", *_CHECKS]
    assert "needs.precheck.result == 'success'" in certify["if"]
    assert "skip_train != 'true'" in certify["if"]
    assert "needs.precheck.outputs.evidence_run != ''" in certify["if"]
    for name in _CHECKS:
        assert f"needs.{name}.result == 'success'" in certify["if"]
    assert certify["permissions"] == {"contents": "write"}

    mint = _step(certify["steps"], "Mint annotated release certificate")
    publish = _step(certify["steps"], "Publish release tag and channel/latest")
    assert "scripts/host_ops/release/certify.py mint" in mint["run"]
    assert "--app-manifest" in mint["run"]
    assert "--android-manifest" in mint["run"]
    assert "refs/tags/channel/latest" in publish["run"]
    assert "gh pr create" not in _RELEASE_GATE.read_text(encoding="utf-8")


def test_certify_triggers_client_packaging_when_ui_app_needs_release() -> None:
    jobs = _yaml(_RELEASE_GATE)["jobs"]
    certify = jobs["certify"]
    assert certify["outputs"]["package_app"] == "${{ steps.mint.outputs.package_app }}"
    assert certify["outputs"]["package_android"] == "${{ steps.mint.outputs.package_android }}"

    app_job = jobs["release-app"]
    assert app_job["needs"] == ["precheck", "certify"]
    assert (
        app_job["if"]
        == "needs.certify.result == 'success' && needs.certify.outputs.package_app == 'true'"
    )
    assert app_job["uses"] == "./.github/workflows/release-app.yml"
    assert app_job["with"] == {
        "release_tag": "${{ needs.certify.outputs.version }}",
        "target_sha": "${{ needs.certify.outputs.sha }}",
        "publish": True,
    }
    assert app_job["secrets"] == "inherit"

    android_job = jobs["release-android"]
    assert android_job["needs"] == ["precheck", "certify"]
    assert (
        android_job["if"]
        == "needs.certify.result == 'success' && needs.certify.outputs.package_android == 'true'"
    )
    assert android_job["uses"] == "./.github/workflows/release-android.yml"
    assert android_job["with"] == {
        "release_tag": "${{ needs.certify.outputs.version }}",
        "target_sha": "${{ needs.certify.outputs.sha }}",
        "publish": True,
    }
    assert android_job["secrets"] == "inherit"


def test_cluster_release_workflows_publish_no_github_release() -> None:
    # meiva:note
    # A certified cluster release is a Git tag plus channel pointers; no consumer in this
    # repository reads a `v*` Release object, and re-creating one per train leaves empty shells
    # and takes the single `Latest` badge away from the unified client release.
    # meiva:link docs/decisions/engineering/tooling/2026-11-07-cluster-releases-stay-git-only.md
    for path, job_name in ((_RELEASE_GATE, "certify"), (_PROMOTE_RELEASE, "promote")):
        steps = _yaml(path)["jobs"][job_name]["steps"]
        assert all("gh release" not in step.get("run", "") for step in steps), path


def test_unified_release_waits_for_successful_client_builds() -> None:
    job = _yaml(_RELEASE_GATE)["jobs"]["publish-client-release"]
    assert set(job["needs"]) == {"certify", "release-app", "release-android"}
    assert "!cancelled() && !failure()" in job["if"]
    assert (
        "needs.release-app.result == 'success' || needs.release-android.result == 'success'"
        in job["if"]
    )
    assert "github.event_name != 'pull_request'" in job["if"]
    assert "inputs.verify_only != true" in job["if"]


def test_unified_release_checks_latest_and_creates_an_immutable_release() -> None:
    steps = _yaml(_RELEASE_GATE)["jobs"]["publish-client-release"]["steps"]
    check = _step(steps, "Check that the client release can become Latest")
    tag = _step(steps, "Ensure the client tag identifies the certified commit")
    publish = _step(steps, "Publish the client Latest release")
    assert "if" not in check
    assert 'scripts.host_ops.release.check_client_release "$CLIENT_VERSION"' in check["run"]
    assert steps.index(check) < steps.index(tag) < steps.index(publish)
    assert "gh release create " in publish["run"]
    assert "--verify-tag --latest" in publish["run"]
    assert "gh release upload" not in publish["run"]
    assert "gh release edit" not in publish["run"]


def test_client_artifact_download_is_scoped_and_tag_namespaces_are_disjoint() -> None:
    workflows = _REPO_ROOT / ".github" / "workflows"
    app = _yaml(workflows / "release-app.yml")
    android = _yaml(workflows / "release-android.yml")
    assert app[True]["push"]["tags"] == ["app-v[0-9]+.[0-9]+.[0-9]+"]
    assert android[True]["push"]["tags"] == ["android-v[0-9]+.[0-9]+.[0-9]+"]
    for workflow, expected in (
        (app, {"pattern": "app-*"}),
        (android, {"name": "android-app"}),
    ):
        downloads = [
            step["with"]
            for step in workflow["jobs"]["publish"]["steps"]
            if step.get("uses", "").startswith("actions/download-artifact@")
        ]
        assert len(downloads) == 1
        assert {
            key: downloads[0][key] for key in ("pattern", "name") if key in downloads[0]
        } == expected


def test_promote_workflow_verifies_certificate_and_updates_channel_stable() -> None:
    workflow = _yaml(_PROMOTE_RELEASE)
    triggers = workflow[True]
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["version"]["required"] is True

    promote_job = workflow["jobs"]["promote"]
    verify_step = _step(
        promote_job["steps"], "Verify release certificate and update channel/stable"
    )
    push_step = _step(promote_job["steps"], "Push channel/stable")
    assert "scripts/host_ops/release/promote.py" in verify_step["run"]
    assert "refs/tags/channel/stable:refs/tags/channel/stable" in push_step["run"]
