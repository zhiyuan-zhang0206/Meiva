from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any, cast

import yaml

from base.host import tool_pins

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_NIGHTLY_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "scheduled-audits.yml"
_PRE_COMMIT = _REPO_ROOT / ".pre-commit-config.yaml"
_POLICY = _REPO_ROOT / ".gitleaks.toml"
_FORBIDDEN_REPORT_FIELDS = ("Secret", "Match", "Message")


def _load_yaml(path: Path) -> dict[object, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[object, Any]", document)


def _assert_no_raw_finding_fields(script: str) -> None:
    for field in _FORBIDDEN_REPORT_FIELDS:
        assert field not in script, f"secret-scan script must never reference {field!r}"


def _assert_secret_scan_job_runs_gitleaks(workflow: dict[object, Any]) -> None:
    secret_scan = workflow["jobs"]["secret-scan"]
    assert secret_scan["name"] == "secret scan (Gitleaks)"
    assert secret_scan["timeout-minutes"] == 35
    assert secret_scan.get("continue-on-error") is not True
    secret_steps = secret_scan["steps"]
    assert "fetch-depth" not in secret_steps[0].get("with", {})
    assert "github.com/zricethezav/gitleaks/v8@v8.30.1" in secret_steps[1]["run"]
    scan = secret_steps[2]["run"]
    for snippet in (
        '--redact --no-banner --report-format json --report-path "$RUNNER_TEMP/gitleaks.json" .',
        "git fetch --no-tags --depth=64 origin",
        "git fetch --no-tags --unshallow origin",
        "unable to resolve base commit for pull request",
        'run_gitleaks --config .gitleaks.toml --log-opts="${base}..HEAD"',
        "git rev-parse --verify HEAD^1",
        "git fetch origin +refs/heads/main:refs/remotes/origin/main",
        "git merge-base -- HEAD refs/remotes/origin/main",
        'run_gitleaks --config .gitleaks.toml --log-opts="${merge_base}..HEAD"',
        'run_gitleaks --config .gitleaks.toml --log-opts="-n 50"',
        "unable to resolve merge-base with origin/main",
        'git diff --name-only "$range_base" HEAD -- .gitleaks.toml',
        '+refs/heads/*:refs/remotes/origin/*" "+refs/tags/*:refs/tags/*"',
        "scan_full_history",
    ):
        assert snippet in scan
    _assert_no_raw_finding_fields(scan)


def _assert_nightly_scan_covers_full_history() -> None:
    nightly = _load_yaml(_NIGHTLY_WORKFLOW)
    triggers = nightly[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    job = nightly["jobs"]["secret-scan-history"]
    assert job["if"].startswith("${{ github.ref == 'refs/heads/main' && ")
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    assert "github.com/zricethezav/gitleaks/v8@v8.30.1" in job["steps"][1]["run"]
    nightly_run = job["steps"][2]["run"]
    assert (
        'gitleaks git --config .gitleaks.toml --redact --no-banner --report-format json --report-path "$RUNNER_TEMP/gitleaks.json" .'
        in nightly_run
    )
    assert "GITHUB_STEP_SUMMARY" in nightly_run
    _assert_no_raw_finding_fields(nightly_run)


def _assert_dependency_audit_job_runs_both_audits(workflow: dict[object, Any]) -> None:
    dependency_audit = workflow["jobs"]["dependency-audit"]
    assert dependency_audit.get("continue-on-error") is not True
    audit_steps = dependency_audit["steps"]
    assert audit_steps[1]["with"]["version"] == tool_pins.UV_VERSION
    assert audit_steps[2]["run"] == "uv audit --frozen"
    assert audit_steps[2]["continue-on-error"] is True
    assert audit_steps[4]["with"]["version"] == tool_pins.PNPM_VERSION
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
        "description": "Weekly planner demo localStorage key",
        "condition": "AND",
        "paths": [r"^demos/goal-mode/weekly-planner/store\.js$"],
        "regexTarget": "match",
        "regexes": [r"STORAGE_KEY = 'weekly-planner\.v1'"],
    }
    assert allowlists[3] == {
        "description": "Historical empty Telegram bot-token placeholder in .env.example",
        "condition": "AND",
        "commits": [
            "40a3e60c3cea5994c402634bde54a5898debe62e",
            "ded34bf876d97fb6c6538bf4c903554ad7e1bb04",
        ],
        "paths": [r"^\.env\.example$"],
        "regexTarget": "match",
        "regexes": [r"(?s)[A-Z]{3,5}_TELEGRAM_BOT_TOKEN=\n[A-Z]{3,5}_TELEGRAM_OWNER_ID=0"],
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
    assert 'run_gitleaks --config .gitleaks.toml --log-opts="${merge_base}..HEAD"' in scan_script
    assert 'run_gitleaks --config .gitleaks.toml --log-opts="-n 50"' in scan_script


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
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    gitleaks_log = tmp_path / "gitleaks.log"
    gitleaks_bin = bin_dir / "gitleaks"
    gitleaks_bin.write_text(
        f'#!/bin/sh\necho "$@" >> "{gitleaks_log}"\n',
        encoding="utf-8",
    )
    gitleaks_bin.chmod(0o755)
    env_base = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
    }

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


# meiva:note
# Asserts that pure location fields appear on stdout and step summary while canary secret/match/message strings never appear anywhere.
def _assert_redacted_projection(
    stdout: str,
    stderr: str,
    summary: Path,
    *,
    canaries: tuple[str, str, str],
    expect_table: bool,
) -> None:
    summary_text = summary.read_text(encoding="utf-8") if summary.exists() else ""
    combined = f"{stdout}\n{stderr}\n{summary_text}"
    for canary in canaries:
        assert canary not in combined
    if not expect_table:
        return
    expected_tokens = (
        "generic-api-key",
        "40a3e60c3cea5994c402634bde54a5898debe62e",
        "2026-08-18T11:22:33Z",
        ".env.example",
        "108-109",
        "40a3e60c3cea5994c402634bde54a5898debe62e:.env.example:generic-api-key:108",
    )
    for token in expected_tokens:
        assert token in stdout
        assert token in summary_text


# meiva:note
# Executes both checked-in workflow bash steps against a stand-in gitleaks binary to verify pure location projection and exit codes.
def test_secret_scan_projects_location_metadata_without_leaking_secret_or_match(
    tmp_path: Path,
) -> None:
    ci_script = _load_yaml(_WORKFLOW)["jobs"]["secret-scan"]["steps"][2]["run"]
    nightly_script = _load_yaml(_NIGHTLY_WORKFLOW)["jobs"]["secret-scan-history"]["steps"][2]["run"]

    origin = tmp_path / "origin"
    base_sha, small_tip, _, _ = _build_origin_with_prs(origin)
    clone = tmp_path / "clone"
    clone.mkdir()
    _git_out(clone, "init")
    _git_out(clone, "remote", "add", "origin", f"file://{origin}")
    _git_out(clone, "fetch", "--depth=1", "origin", small_tip)
    _git_out(clone, "checkout", "--detach", "FETCH_HEAD")

    canary_raw = "POISON_RAW_SECRET_9918273"
    canary_match = "POISON_RAW_MATCH_LINE_88271"
    canary_message = "POISON_COMMIT_MESSAGE_77162"
    findings = [
        {
            "RuleID": "generic-api-key",
            "Commit": "40a3e60c3cea5994c402634bde54a5898debe62e",
            "Date": "2026-08-18T11:22:33Z",
            "File": ".env.example",
            "StartLine": 108,
            "EndLine": 109,
            "Fingerprint": "40a3e60c3cea5994c402634bde54a5898debe62e:.env.example:generic-api-key:108",
            "Secret": canary_raw,
            "Match": canary_match,
            "Message": canary_message,
        }
    ]
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(findings), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gitleaks_bin = bin_dir / "gitleaks"
    gitleaks_bin.write_text(
        "#!/bin/sh\n"
        'prev=""\n'
        'report=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "--report-path" ]; then report="$arg"; fi\n'
        '  prev="$arg"\n'
        "done\n"
        f'if [ "$WRITE_REPORT" = "1" ] && [ -n "$report" ]; then cp "{payload_file}" "$report"; fi\n'
        'exit "${FAKE_RC:-1}"\n',
        encoding="utf-8",
    )
    gitleaks_bin.chmod(0o755)

    for label, script, extra_env, expected_rc in (
        ("nightly", nightly_script, {"FAKE_RC": "1", "WRITE_REPORT": "1"}, 1),
        (
            "ci-pr",
            ci_script,
            {"EVENT": "pull_request", "BASE_SHA": base_sha, "FAKE_RC": "2", "WRITE_REPORT": "1"},
            2,
        ),
        ("nightly-no-report", nightly_script, {"FAKE_RC": "3", "WRITE_REPORT": "0"}, 3),
    ):
        runner_temp = tmp_path / f"temp-{label}"
        runner_temp.mkdir()
        summary = tmp_path / f"summary-{label}.md"
        res = subprocess.run(  # noqa: S603
            ["bash", "-eo", "pipefail", "-c", script],
            cwd=clone,
            env=os.environ
            | {
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_STEP_SUMMARY": str(summary),
            }
            | extra_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == expected_rc, (label, res.stdout, res.stderr)
        assert not (runner_temp / "gitleaks.json").exists()
        _assert_redacted_projection(
            res.stdout,
            res.stderr,
            summary,
            canaries=(canary_raw, canary_match, canary_message),
            expect_table=extra_env["WRITE_REPORT"] == "1",
        )


# meiva:note
# Builds an origin repository deep enough that shallow depth-64 fetches remain shallow unless .gitleaks.toml changes widen them.
def _seed_policy_origin(origin: Path) -> tuple[tuple[str, str, str, str, bool, bool], ...]:
    origin.mkdir()
    _git_out(origin, "init", "-b", "main")
    _git_out(origin, "config", "user.email", "test@example.com")
    _git_out(origin, "config", "user.name", "Test")
    _git_out(origin, "config", "uploadpack.allowReachableSHA1InWant", "true")
    _git_out(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    (origin / "app.txt").write_text("v1\n", encoding="utf-8")
    _git_out(origin, "add", "app.txt")
    _git_out(origin, "commit", "-m", "initial without policy")
    no_policy_base = _git_out(origin, "rev-parse", "HEAD")
    _git_out(origin, "tag", "v0.1.0", no_policy_base)

    _git_out(origin, "checkout", "-b", "pr-add", no_policy_base)
    (origin / ".gitleaks.toml").write_text('title = "added"\n', encoding="utf-8")
    _git_out(origin, "add", ".gitleaks.toml")
    _git_out(origin, "commit", "-m", "add policy")
    pr_add_tip = _git_out(origin, "rev-parse", "HEAD")

    _git_out(origin, "checkout", "main")
    (origin / ".gitleaks.toml").write_text('title = "base"\n', encoding="utf-8")
    _git_out(origin, "add", ".gitleaks.toml")
    _git_out(origin, "commit", "-m", "main policy")
    blob = _git_out(origin, "hash-object", "-w", "--stdin", stdin="x\n")
    policy_blob = _git_out(origin, "rev-parse", "HEAD:.gitleaks.toml")
    tip = _git_out(origin, "rev-parse", "HEAD")
    for idx in range(70):
        entries = f"100644 blob {policy_blob}\t.gitleaks.toml\n100644 blob {blob}\tm{idx}.txt\n"
        tree = _git_out(origin, "mktree", stdin=entries)
        tip = _git_out(origin, "commit-tree", tree, "-p", tip, "-m", f"main {idx}")
    _git_out(origin, "reset", "--hard", tip)
    policy_base = _git_out(origin, "rev-parse", "HEAD")

    _git_out(origin, "checkout", "-b", "side-branch", policy_base)
    (origin / "side.txt").write_text("side\n", encoding="utf-8")
    _git_out(origin, "add", "side.txt")
    _git_out(origin, "commit", "-m", "side commit")

    _git_out(origin, "checkout", "-b", "pr-modify", policy_base)
    (origin / ".gitleaks.toml").write_text('title = "modified"\n', encoding="utf-8")
    _git_out(origin, "add", ".gitleaks.toml")
    _git_out(origin, "commit", "-m", "modify policy")
    pr_modify_tip = _git_out(origin, "rev-parse", "HEAD")

    _git_out(origin, "checkout", "-b", "pr-delete", policy_base)
    _git_out(origin, "rm", ".gitleaks.toml")
    _git_out(origin, "commit", "-m", "delete policy")
    pr_delete_tip = _git_out(origin, "rev-parse", "HEAD")

    _git_out(origin, "checkout", "-b", "pr-untouched", policy_base)
    (origin / "app.txt").write_text("feature\n", encoding="utf-8")
    _git_out(origin, "add", "app.txt")
    _git_out(origin, "commit", "-m", "ordinary change")
    pr_untouched_tip = _git_out(origin, "rev-parse", "HEAD")
    _git_out(origin, "checkout", "main")
    return (
        ("pr-add", no_policy_base, pr_add_tip, "false", True, True),
        ("pr-modify", policy_base, pr_modify_tip, "false", True, True),
        ("pr-delete", policy_base, pr_delete_tip, "false", True, False),
        ("pr-untouched", policy_base, pr_untouched_tip, "true", False, True),
    )


# meiva:note
# Verifies ci.yml widens shallow pull-request scans to unshallowed full-history scans when .gitleaks.toml is added, modified, or deleted.
def test_secret_scan_widens_to_full_history_when_gitleaks_policy_changes(tmp_path: Path) -> None:
    scan_script = _load_yaml(_WORKFLOW)["jobs"]["secret-scan"]["steps"][2]["run"]
    origin = tmp_path / "origin"
    cases = _seed_policy_origin(origin)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    gitleaks_log = tmp_path / "gitleaks.log"
    gitleaks_bin = bin_dir / "gitleaks"
    gitleaks_bin.write_text(
        f'#!/bin/sh\necho "$@" >> "{gitleaks_log}"\n',
        encoding="utf-8",
    )
    gitleaks_bin.chmod(0o755)
    env_base = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
    }

    for name, base_ref, tip_ref, expect_shallow, expect_widen, expect_config in cases:
        clone = tmp_path / name
        clone.mkdir()
        _git_out(clone, "init")
        _git_out(clone, "remote", "add", "origin", f"file://{origin}")
        _git_out(clone, "fetch", "--depth=1", "origin", tip_ref)
        _git_out(clone, "checkout", "--detach", "FETCH_HEAD")
        gitleaks_log.unlink(missing_ok=True)
        res = subprocess.run(  # noqa: S603
            ["bash", "-eo", "pipefail", "-c", scan_script],
            cwd=clone,
            env=env_base | {"EVENT": "pull_request", "BASE_SHA": base_ref},
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, (name, res.stdout, res.stderr)
        assert _git_out(clone, "rev-parse", "--is-shallow-repository") == expect_shallow
        logged = gitleaks_log.read_text(encoding="utf-8").strip()
        assert ("--log-opts=" not in logged) is expect_widen, (name, logged)
        assert ("--config .gitleaks.toml" in logged) is expect_config, (name, logged)
        if expect_widen:
            assert "v0.1.0" in _git_out(clone, "tag", "-l")
            assert "origin/side-branch" in _git_out(clone, "branch", "-r")


# meiva:note
# Seeds main-branch dispatch fixtures covering root policy, >50-commit aged policy, in-window touch-then-revert, and in-window policy merges.
def _seed_main_window_cases(base_dir: Path) -> tuple[tuple[str, Path, bool], ...]:
    root_origin = base_dir / "origin-root"
    root_origin.mkdir()
    _git_out(root_origin, "init", "-b", "main")
    _git_out(root_origin, "config", "user.email", "test@example.com")
    _git_out(root_origin, "config", "user.name", "Test")
    (root_origin / ".gitleaks.toml").write_text('title = "root"\n', encoding="utf-8")
    _git_out(root_origin, "add", ".gitleaks.toml")
    _git_out(root_origin, "commit", "-m", "root policy")

    aged_origin = base_dir / "origin-aged"
    aged_origin.mkdir()
    _git_out(aged_origin, "init", "-b", "main")
    _git_out(aged_origin, "config", "user.email", "test@example.com")
    _git_out(aged_origin, "config", "user.name", "Test")
    (aged_origin / ".gitleaks.toml").write_text('title = "v1"\n', encoding="utf-8")
    _git_out(aged_origin, "add", ".gitleaks.toml")
    _git_out(aged_origin, "commit", "-m", "old policy")
    early_base = _git_out(aged_origin, "rev-parse", "HEAD")
    blob = _git_out(aged_origin, "hash-object", "-w", "--stdin", stdin="x\n")
    policy_blob = _git_out(aged_origin, "rev-parse", "HEAD:.gitleaks.toml")
    tip = early_base
    for idx in range(60):
        entries = f"100644 blob {policy_blob}\t.gitleaks.toml\n100644 blob {blob}\tn{idx}.txt\n"
        tree = _git_out(aged_origin, "mktree", stdin=entries)
        tip = _git_out(aged_origin, "commit-tree", tree, "-p", tip, "-m", f"main {idx}")
    _git_out(aged_origin, "reset", "--hard", tip)

    revert_origin = base_dir / "origin-revert"
    _git_out(base_dir, "clone", "--quiet", str(aged_origin), str(revert_origin))
    _git_out(revert_origin, "config", "user.email", "test@example.com")
    _git_out(revert_origin, "config", "user.name", "Test")
    (revert_origin / ".gitleaks.toml").write_text('title = "transient"\n', encoding="utf-8")
    _git_out(revert_origin, "add", ".gitleaks.toml")
    _git_out(revert_origin, "commit", "-m", "touch policy in window")
    (revert_origin / ".gitleaks.toml").write_text('title = "v1"\n', encoding="utf-8")
    _git_out(revert_origin, "add", ".gitleaks.toml")
    _git_out(revert_origin, "commit", "-m", "revert policy in window")

    merge_origin = base_dir / "origin-merge"
    _git_out(base_dir, "clone", "--quiet", str(aged_origin), str(merge_origin))
    _git_out(merge_origin, "config", "user.email", "test@example.com")
    _git_out(merge_origin, "config", "user.name", "Test")
    old_policy_blob = _git_out(
        merge_origin, "hash-object", "-w", "--stdin", stdin='title = "merged"\n'
    )
    branch_tree = _git_out(
        merge_origin, "mktree", stdin=f"100644 blob {old_policy_blob}\t.gitleaks.toml\n"
    )
    old_env = os.environ | {
        "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z",
    }
    branch_commit = subprocess.run(  # noqa: S603
        ["git", "commit-tree", branch_tree, "-p", early_base, "-m", "old dated policy branch"],
        cwd=merge_origin,
        env=old_env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git_out(merge_origin, "merge", "--no-ff", "-m", "merge policy branch", branch_commit)
    return (
        ("root-policy", root_origin, True),
        ("aged-policy-gt50", aged_origin, False),
        ("window-touch-revert", revert_origin, True),
        ("window-merge-policy", merge_origin, True),
    )


# meiva:note
# Verifies workflow_dispatch on main bounds policy checks to the 50-commit window, covering root, >50 aged policy, touch-then-revert, and merges.
def test_secret_scan_main_dispatch_bounds_policy_check_to_recent_50_commits(
    tmp_path: Path,
) -> None:
    scan_script = _load_yaml(_WORKFLOW)["jobs"]["secret-scan"]["steps"][2]["run"]
    cases = _seed_main_window_cases(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    gitleaks_log = tmp_path / "gitleaks.log"
    gitleaks_bin = bin_dir / "gitleaks"
    gitleaks_bin.write_text(f'#!/bin/sh\necho "$@" >> "{gitleaks_log}"\n', encoding="utf-8")
    gitleaks_bin.chmod(0o755)
    env_base = os.environ | {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
        "EVENT": "workflow_dispatch",
        "BASE_SHA": "",
    }

    for name, origin, expect_widen in cases:
        clone = tmp_path / f"clone-{name}"
        clone.mkdir()
        _git_out(clone, "init")
        _git_out(clone, "remote", "add", "origin", f"file://{origin}")
        _git_out(clone, "fetch", "--depth=1", "origin", "main")
        _git_out(clone, "checkout", "--detach", "FETCH_HEAD")
        gitleaks_log.unlink(missing_ok=True)
        res = subprocess.run(  # noqa: S603
            ["bash", "-eo", "pipefail", "-c", scan_script],
            cwd=clone,
            env=env_base,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, (name, res.stdout, res.stderr)
        logged = gitleaks_log.read_text(encoding="utf-8").strip()
        if expect_widen:
            assert "--log-opts=" not in logged, (name, logged)
        else:
            assert "--log-opts=-n 50" in logged, (name, logged)
