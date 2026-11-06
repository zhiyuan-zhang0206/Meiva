from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.host_ops.release import evidence
from scripts.host_ops.release.git_tags import git_run

_REPO_NAME = "owner/project"
_NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _git(repo: Path, *arguments: str) -> str:
    result = git_run(repo, *arguments)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _ = _git(tmp_path, "init", "-q", "-b", "main")
    _ = _git(tmp_path, "config", "user.email", "test@example.invalid")
    _ = _git(tmp_path, "config", "user.name", "test")
    (tmp_path / evidence.WORKFLOW).parent.mkdir(parents=True)
    for path in (evidence.WORKFLOW, "uv.lock"):
        _ = (tmp_path / path).write_text("version: 1\n", encoding="utf-8")
    _ = _git(tmp_path, "add", ".")
    _ = _git(tmp_path, "commit", "-qm", "Source snapshot")
    _ = _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return tmp_path


def _run(repo: Path, revision: str) -> dict[str, object]:
    sha = _git(repo, "rev-parse", revision)
    return {
        "id": 100,
        "head_sha": sha,
        "head_branch": "main",
        "path": evidence.WORKFLOW,
        "display_title": f"Verify release tree {sha}",
        "event": "workflow_dispatch",
        "repository": {"full_name": _REPO_NAME},
        "head_repository": {"full_name": _REPO_NAME},
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "run_started_at": "2026-11-07T11:15:00Z",
    }


def test_complete_main_verification_applies_to_the_same_tree_after_commit_changes(
    repository: Path,
) -> None:
    run = _run(repository, "HEAD")
    _ = _git(repository, "commit", "--allow-empty", "-qm", "Same content, different commit")
    _ = _git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    sha = _git(repository, "rev-parse", "HEAD")

    assert sha != run["head_sha"]
    assert evidence.verified_run(repository, _REPO_NAME, sha, _NOW, {"workflow_runs": [run]}) == 100


@pytest.mark.parametrize("path", ["uv.lock", evidence.WORKFLOW])
def test_changed_verification_inputs_require_a_fresh_suite(repository: Path, path: str) -> None:
    run = _run(repository, "HEAD")
    _ = (repository / path).write_text("version: 2\n", encoding="utf-8")
    _ = _git(repository, "commit", "-qam", "Change verification inputs")
    _ = _git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    sha = _git(repository, "rev-parse", "HEAD")

    assert (
        evidence.verified_run(repository, _REPO_NAME, sha, _NOW, {"workflow_runs": [run]}) is None
    )


def test_matching_target_still_needs_the_current_suite_definition(repository: Path) -> None:
    run = _run(repository, "HEAD")
    sha = _git(repository, "rev-parse", "HEAD")
    _ = (repository / evidence.WORKFLOW).write_text("version: 2\n", encoding="utf-8")
    _ = _git(repository, "commit", "-qam", "Upgrade runner")

    assert (
        evidence.verified_run(repository, _REPO_NAME, sha, _NOW, {"workflow_runs": [run]}) is None
    )


def test_evidence_from_an_unmerged_commit_cannot_certify_main(repository: Path) -> None:
    _ = _git(repository, "commit", "--allow-empty", "-qm", "Unmerged same-tree commit")
    sha = _git(repository, "rev-parse", "HEAD^")
    run = _run(repository, "HEAD")

    assert (
        evidence.verified_run(repository, _REPO_NAME, sha, _NOW, {"workflow_runs": [run]}) is None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("head_repository", {"full_name": "fork/project"}),
        ("repository", {"full_name": "fork/project"}),
        ("head_branch", "topic"),
        ("path", ".github/workflows/ci.yml"),
        ("event", "pull_request"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("conclusion", "cancelled"),
        ("conclusion", "skipped"),
        ("run_attempt", 2),
        ("run_started_at", "2026-11-06T11:59:59Z"),
        ("run_started_at", "2026-11-07T12:00:01Z"),
        ("run_started_at", "invalid"),
        ("display_title", "Certify release"),
    ],
)
def test_incomplete_stale_or_untrusted_runs_are_misses(
    repository: Path, field: str, value: object
) -> None:
    sha = _git(repository, "rev-parse", "HEAD")
    run = _run(repository, sha)
    run[field] = value

    assert (
        evidence.verified_run(repository, _REPO_NAME, sha, _NOW, {"workflow_runs": [run]}) is None
    )


def test_a_newer_matching_failure_invalidates_the_older_pass(repository: Path) -> None:
    sha = _git(repository, "rev-parse", "HEAD")
    older = _run(repository, sha)
    newer = older | {"id": 101, "conclusion": "failure", "run_started_at": "2026-11-07T11:30:00Z"}

    assert (
        evidence.verified_run(repository, _REPO_NAME, sha, _NOW, {"workflow_runs": [newer, older]})
        is None
    )
