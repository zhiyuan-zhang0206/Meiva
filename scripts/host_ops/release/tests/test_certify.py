from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.host_ops.release import certify, promote
from scripts.host_ops.release.git_tags import (
    read_verified_certificate,
    read_verified_channel,
)


def _git(repo: Path, *arguments: str) -> str:
    done = subprocess.run(  # noqa: S603
        ("git", "-C", str(repo), *arguments), check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "app.py").write_text("V = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c1")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_release_train_mints_calver_advances_latest_and_skips_when_idle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, sha1 = _repository(tmp_path)
    gh_out = tmp_path / "github_output.txt"

    rc = certify.main(
        [
            "precheck",
            "--repo",
            str(repo),
            "--event-name",
            "schedule",
            "--github-output",
            str(gh_out),
        ]
    )
    assert rc == 0
    pre1 = json.loads(capsys.readouterr().out)
    assert pre1["skip_train"] == "false" and pre1["sha"] == sha1
    assert "skip_train=false" in gh_out.read_text(encoding="utf-8")

    rc = certify.main(
        [
            "mint",
            "--repo",
            str(repo),
            "--sha",
            sha1,
            "--source-ref",
            "refs/heads/main",
            "--certified-at",
            "2026-11-01T14:30:22Z",
            "--gate-run-id",
            "501",
            "--gate-run-url",
            "https://github.com/o/r/actions/runs/501",
        ]
    )
    assert rc == 0
    minted = json.loads(capsys.readouterr().out)
    assert minted["version"] == "v2026.11.01.143022"
    assert minted["advance_latest"] == "true"

    cert, status = read_verified_certificate(repo, "v2026.11.01.143022")
    assert cert is not None and status == "verified" and cert.sha == sha1
    ptr, ch_cert, ch_status = read_verified_channel(repo, "latest")
    assert ptr is not None and ch_cert == cert and ch_status == "verified"

    rc = certify.main(["precheck", "--repo", str(repo), "--event-name", "schedule"])
    assert rc == 0
    idle = json.loads(capsys.readouterr().out)
    assert idle["skip_train"] == "true"
    assert idle["existing_version"] == "v2026.11.01.143022"


def test_hotfix_certifies_hf_tag_without_moving_latest_and_can_promote_to_stable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, sha1 = _repository(tmp_path)
    assert (
        certify.main(
            [
                "mint",
                "--repo",
                str(repo),
                "--sha",
                sha1,
                "--source-ref",
                "refs/heads/main",
                "--certified-at",
                "2026-11-01T12:00:00Z",
                "--gate-run-id",
                "10",
                "--gate-run-url",
                "https://github.com/o/r/actions/runs/10",
            ]
        )
        == 0
    )
    capsys.readouterr()

    (repo / "app.py").write_text("V = 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main-next")
    sha2 = _git(repo, "rev-parse", "HEAD")
    assert (
        certify.main(
            [
                "mint",
                "--repo",
                str(repo),
                "--sha",
                sha2,
                "--source-ref",
                "refs/heads/main",
                "--certified-at",
                "2026-11-01T18:00:00Z",
                "--gate-run-id",
                "11",
                "--gate-run-url",
                "https://github.com/o/r/actions/runs/11",
            ]
        )
        == 0
    )
    capsys.readouterr()

    _git(repo, "checkout", "-q", "-b", "hotfix/urgent", sha1)
    (repo / "hotfix.txt").write_text("patch\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "urgent patch")
    hf_sha = _git(repo, "rev-parse", "HEAD")

    assert (
        certify.main(
            [
                "mint",
                "--repo",
                str(repo),
                "--sha",
                hf_sha,
                "--source-ref",
                "refs/heads/hotfix/urgent",
                "--certified-at",
                "2026-11-01T19:00:00Z",
                "--gate-run-id",
                "12",
                "--gate-run-url",
                "https://github.com/o/r/actions/runs/12",
                "--hotfix-of",
                "v2026.11.01.120000",
                "--reason",
                "fix regression",
            ]
        )
        == 0
    )
    hf_out = json.loads(capsys.readouterr().out)
    assert hf_out["version"] == "v2026.11.01.120000-hf1"
    assert hf_out["advance_latest"] == "false"

    latest_ptr, _, _ = read_verified_channel(repo, "latest")
    assert latest_ptr is not None and latest_ptr.version == "v2026.11.01.180000"

    assert (
        promote.main(
            [
                "--repo",
                str(repo),
                "--version",
                "v2026.11.01.120000-hf1",
                "--updated-at",
                "2026-11-01T19:05:00Z",
                "--reason",
                "emergency stable hotfix",
            ]
        )
        == 0
    )
    capsys.readouterr()
    stable_ptr, stable_cert, _ = read_verified_channel(repo, "stable")
    assert stable_ptr is not None and stable_cert is not None
    assert stable_ptr.version == "v2026.11.01.120000-hf1" and stable_cert.sha == hf_sha


def test_certify_rejects_hotfix_from_non_ancestor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, sha1 = _repository(tmp_path)
    _git(repo, "checkout", "-q", "--orphan", "other")
    (repo / "other.txt").write_text("o\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "orphan")
    orphan_sha = _git(repo, "rev-parse", "HEAD")

    assert (
        certify.main(
            [
                "mint",
                "--repo",
                str(repo),
                "--sha",
                sha1,
                "--source-ref",
                "refs/heads/main",
                "--certified-at",
                "2026-11-01T12:00:00Z",
                "--gate-run-id",
                "1",
                "--gate-run-url",
                "https://github.com/o/r/actions/runs/1",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        certify.main(
            [
                "precheck",
                "--repo",
                str(repo),
                "--target-ref",
                orphan_sha,
                "--hotfix-of",
                "v2026.11.01.120000",
            ]
        )
        == 1
    )
    err = capsys.readouterr()
    assert "does not descend from --hotfix-of" in err.err
