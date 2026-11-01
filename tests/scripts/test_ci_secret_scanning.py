from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any, cast

import yaml

from base.host import brew_pin

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_NIGHTLY_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "secret-scan-history.yml"
_PRE_COMMIT = _REPO_ROOT / ".pre-commit-config.yaml"
_POLICY = _REPO_ROOT / ".gitleaks.toml"


def _load_yaml(path: Path) -> dict[object, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[object, Any]", document)


def _assert_secret_scan_job_runs_gitleaks(workflow: dict[object, Any]) -> None:
    secret_scan = workflow["jobs"]["secret-scan"]
    assert secret_scan["name"] == "secret scan (Gitleaks)"
    assert secret_scan.get("continue-on-error") is not True
    secret_steps = secret_scan["steps"]
    assert "fetch-depth" not in secret_steps[0].get("with", {})
    assert "github.com/zricethezav/gitleaks/v8@v8.30.1" in secret_steps[1]["run"]
    scan = secret_steps[2]["run"]
    for snippet in (
        "git fetch --no-tags --depth=64 origin",
        "git fetch --no-tags --unshallow origin",
        "unable to resolve base commit for pull request",
        'gitleaks git --config .gitleaks.toml --redact --no-banner --log-opts="${base}..HEAD" .',
        "git rev-parse --verify HEAD^1",
        "git fetch origin +refs/heads/main:refs/remotes/origin/main",
        "git merge-base -- HEAD refs/remotes/origin/main",
        'gitleaks git --config .gitleaks.toml --redact --no-banner --log-opts="${merge_base}..HEAD" .',
        'gitleaks git --config .gitleaks.toml --redact --no-banner --log-opts="-n 50" .',
        "unable to resolve merge-base with origin/main",
    ):
        assert snippet in scan
    assert "gitleaks git --config .gitleaks.toml --redact --no-banner .\n" not in scan


def _assert_nightly_scan_covers_full_history() -> None:
    nightly = _load_yaml(_NIGHTLY_WORKFLOW)
    triggers = nightly[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    job = nightly["jobs"]["secret-scan-history"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    assert "github.com/zricethezav/gitleaks/v8@v8.30.1" in job["steps"][1]["run"]
    assert job["steps"][2]["run"] == "gitleaks git --config .gitleaks.toml --redact --no-banner ."


def _assert_dependency_audit_job_runs_both_audits(workflow: dict[object, Any]) -> None:
    dependency_audit = workflow["jobs"]["dependency-audit"]
    assert dependency_audit.get("continue-on-error") is not True
    audit_steps = dependency_audit["steps"]
    assert audit_steps[1]["with"]["version"] == brew_pin.UV_VERSION
    assert audit_steps[2]["run"] == "uv audit --frozen"
    assert audit_steps[2]["continue-on-error"] is True
    assert audit_steps[4]["with"]["version"] == brew_pin.PNPM_VERSION
    assert audit_steps[5]["working-directory"] == "ui/web"
    assert audit_steps[5]["run"] == "pnpm audit"
    assert audit_steps[5]["continue-on-error"] is True


def _assert_pre_commit_uses_the_gitleaks_policy() -> None:
    pre_commit = _load_yaml(_PRE_COMMIT)
    hooks = [hook for repo in pre_commit["repos"] for hook in repo["hooks"]]
    assert "detect-private-key" not in {hook["id"] for hook in hooks}
    gitleaks_hook = next(hook for hook in hooks if hook["id"] == "gitleaks")
    assert gitleaks_hook["args"] == ["--config=.gitleaks.toml"]


def _assert_gitleaks_policy_allowlists() -> None:
    with _POLICY.open("rb") as policy_file:
        policy = tomllib.load(policy_file)
    assert policy["extend"]["useDefault"] is True
    allowlists = policy["allowlists"]
    fixture_allowlist = allowlists[0]
    allowlisted_regexes = "\n".join(fixture_allowlist["regexes"])
    for fixture in ("meiva_dev_only", "pw123", "s3cr3t", "new-db", "new-runner-db"):
        assert fixture in allowlisted_regexes
    assert set(fixture_allowlist["commits"]) == {
        "e0270b05d28524401b6caf5b0a300ab91a4d1c6a",
        "14e008cd0f9ab4b791330f16d098d8155726d392",
        "c15f00344d4805b82ed2e0cda824a7527cc0b943",
    }
    assert allowlists[1] == {
        "description": "Empty Telegram bot-token placeholder in the environment template",
        "condition": "AND",
        "paths": [r"^\.env\.example$"],
        "regexTarget": "match",
        "regexes": [r"(?s)[A-Z]{3,5}_TELEGRAM_BOT_TOKEN=\n[A-Z]{3,5}_TELEGRAM_OWNER_ID=0"],
    }
    assert allowlists[2] == {
        "description": "Weekly planner demo localStorage key",
        "condition": "AND",
        "paths": [r"^demos/goal-mode/weekly-planner/store\.js$"],
        "regexTarget": "match",
        "regexes": [r"STORAGE_KEY = 'weekly-planner\.v1'"],
    }


def test_ci_secret_and_dependency_audits_are_wired_to_one_policy() -> None:
    workflow = _load_yaml(_WORKFLOW)
    _assert_secret_scan_job_runs_gitleaks(workflow)
    _assert_nightly_scan_covers_full_history()
    _assert_dependency_audit_job_runs_both_audits(workflow)
    _assert_pre_commit_uses_the_gitleaks_policy()
    _assert_gitleaks_policy_allowlists()

    assert not (_REPO_ROOT / ".gitguardian.yml").exists()


def test_secret_scan_workflow_dispatch_contract() -> None:
    workflow = _load_yaml(_WORKFLOW)
    secret_scan = workflow["jobs"]["secret-scan"]
    scan_script = secret_scan["steps"][2]["run"]
    env = secret_scan["steps"][2]["env"]

    assert env["EVENT"] == "${{ github.event_name }}"
    assert 'if [ "$EVENT" = "pull_request" ]; then' in scan_script
    assert "git fetch origin +refs/heads/main:refs/remotes/origin/main" in scan_script
    assert "git merge-base -- HEAD refs/remotes/origin/main" in scan_script
    assert ': "${merge_base:?unable to resolve merge-base with origin/main}"' in scan_script
    assert 'if [ "$merge_base" != "$head_sha" ]; then' in scan_script
    assert (
        'gitleaks git --config .gitleaks.toml --redact --no-banner --log-opts="${merge_base}..HEAD" .'
        in scan_script
    )
    assert (
        'gitleaks git --config .gitleaks.toml --redact --no-banner --log-opts="-n 50" .'
        in scan_script
    )
    assert "gitleaks git --config .gitleaks.toml --redact --no-banner .\n" not in scan_script


# meiva:note
# Executes fixed git commands inside temporary fixture repositories owned by the test.
def _git_out(cwd: Path, *args: str, stdin: str | None = None) -> str:
    res = subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _build_origin_with_prs(origin: Path) -> tuple[str, str, str, str]:
    origin.mkdir()
    _git_out(origin, "init", "-b", "main")
    _git_out(origin, "config", "user.email", "test@example.com")
    _git_out(origin, "config", "user.name", "Test")
    _git_out(origin, "config", "uploadpack.allowReachableSHA1InWant", "true")
    _git_out(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    blob = _git_out(origin, "hash-object", "-w", "--stdin", stdin="x\n")
    shas: list[str] = []
    parent: str | None = None
    for idx in range(90):
        tree = _git_out(origin, "mktree", stdin=f"100644 blob {blob}\tf{idx}.txt\n")
        parents = ["-p", parent] if parent else []
        parent = _git_out(origin, "commit-tree", tree, *parents, "-m", f"main {idx}")
        shas.append(parent)
    base_sha = shas[-1]
    _git_out(origin, "update-ref", "refs/heads/main", base_sha)

    def _make_pr(fork_sha: str, count: int, number: int) -> tuple[str, str]:
        tip = fork_sha
        tree = ""
        for step in range(count):
            tree = _git_out(origin, "mktree", stdin=f"100644 blob {blob}\tpr_{number}_{step}.txt\n")
            tip = _git_out(origin, "commit-tree", tree, "-p", tip, "-m", f"pr {step}")
        merge = _git_out(origin, "commit-tree", tree, "-p", base_sha, "-p", tip, "-m", "merge")
        _git_out(origin, "update-ref", f"refs/pull/{number}/merge", merge)
        _git_out(origin, "update-ref", f"refs/pull/{number}/head", tip)
        return tip, merge

    small_tip, small_merge = _make_pr(shas[-8], 2, 1)
    _, deep_merge = _make_pr(shas[5], 2, 2)
    return base_sha, small_tip, small_merge, deep_merge


# meiva:note
# Runs the checked-in ci.yml shell step against temporary shallow git repositories and a local stand-in gitleaks script.
def test_secret_scan_pull_request_shallow_fetch_and_fail_closed(tmp_path: Path) -> None:
    scan_script = _load_yaml(_WORKFLOW)["jobs"]["secret-scan"]["steps"][2]["run"]
    origin = tmp_path / "origin"
    base_sha, small_tip, small_merge, deep_merge = _build_origin_with_prs(origin)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gitleaks_log = tmp_path / "gitleaks.log"
    gitleaks_bin = bin_dir / "gitleaks"
    gitleaks_bin.write_text(
        f'#!/bin/sh\necho "$@" >> "{gitleaks_log}"\n',
        encoding="utf-8",
    )
    gitleaks_bin.chmod(0o755)
    env_base = os.environ | {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    for name, ref, expect_shallow, expect_commits in (
        ("small-merge", small_merge, "true", 3),
        ("deep-merge", deep_merge, "false", 3),
        ("small-head", small_tip, "true", 2),
    ):
        clone = tmp_path / name
        clone.mkdir()
        _git_out(clone, "init")
        _git_out(clone, "remote", "add", "origin", f"file://{origin}")
        _git_out(clone, "fetch", "--depth=1", "origin", ref)
        _git_out(clone, "checkout", "--detach", "FETCH_HEAD")
        gitleaks_log.unlink(missing_ok=True)
        res = subprocess.run(  # noqa: S603
            ["bash", "-eo", "pipefail", "-c", scan_script],
            cwd=clone,
            env=env_base | {"EVENT": "pull_request", "BASE_SHA": base_sha},
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, res.stderr
        assert _git_out(clone, "rev-parse", "--is-shallow-repository") == expect_shallow
        logged = gitleaks_log.read_text(encoding="utf-8").strip()
        range_arg = next(
            part.removeprefix("--log-opts=")
            for part in logged.split()
            if part.startswith("--log-opts=")
        )
        assert int(_git_out(clone, "rev-list", "--count", range_arg)) == expect_commits

    broken = tmp_path / "broken"
    broken.mkdir()
    _git_out(broken, "init")
    _git_out(broken, "remote", "add", "origin", f"file://{origin}")
    _git_out(broken, "fetch", "--depth=1", "origin", small_tip)
    _git_out(broken, "checkout", "--detach", "FETCH_HEAD")
    gitleaks_log.unlink(missing_ok=True)
    failed = subprocess.run(  # noqa: S603
        ["bash", "-eo", "pipefail", "-c", scan_script],
        cwd=broken,
        env=env_base | {"EVENT": "pull_request", "BASE_SHA": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode != 0
    assert "unable to resolve base commit for pull request" in failed.stderr
    assert not gitleaks_log.exists()
