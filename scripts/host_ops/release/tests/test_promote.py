from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.host_ops.release import certify, promote
from scripts.host_ops.release.git_tags import read_verified_channel


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
    (repo / "f.txt").write_text("1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_promote_refuses_missing_lightweight_or_handwritten_tags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, sha = _repository(tmp_path)
    _git(repo, "tag", "v2026.11.01.120000", sha)
    _git(repo, "tag", "-a", "v2026.11.01.120001", "-m", "manual", sha)

    for bad_tag in (
        "v1.0.0",
        "v2026.11.01.999999",
        "v2026.11.01.235959",
        "v2026.11.01.120000",
        "v2026.11.01.120001",
    ):
        rc = promote.main(
            [
                "--repo",
                str(repo),
                "--version",
                bad_tag,
                "--updated-at",
                "2026-11-01T13:00:00Z",
            ]
        )
        assert rc == 2, bad_tag
        capsys.readouterr()

    ptr, cert, _ = read_verified_channel(repo, "stable")
    assert ptr is None and cert is None


def test_promote_dry_run_verifies_without_mutating_channel_stable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, sha = _repository(tmp_path)
    assert (
        certify.main(
            [
                "mint",
                "--repo",
                str(repo),
                "--sha",
                sha,
                "--source-ref",
                "refs/heads/main",
                "--certified-at",
                "2026-11-01T12:00:00Z",
                "--gate-run-id",
                "77",
                "--gate-run-url",
                "https://github.com/o/r/actions/runs/77",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        promote.main(
            [
                "--repo",
                str(repo),
                "--version",
                "v2026.11.01.120000",
                "--updated-at",
                "2026-11-01T14:00:00Z",
                "--dry-run",
            ]
        )
        == 0
    )
    dry = json.loads(capsys.readouterr().out)
    assert dry["ok"] is True and dry["dry_run"] is True
    ptr, _, _ = read_verified_channel(repo, "stable")
    assert ptr is None

    gh_out = tmp_path / "out.txt"
    assert (
        promote.main(
            [
                "--repo",
                str(repo),
                "--version",
                "v2026.11.01.120000",
                "--updated-at",
                "2026-11-01T14:00:00Z",
                "--reason",
                "soaked 2h",
                "--github-output",
                str(gh_out),
            ]
        )
        == 0
    )
    live = json.loads(capsys.readouterr().out)
    assert live["ok"] is True and live["dry_run"] is False
    ptr, cert, status = read_verified_channel(repo, "stable")
    assert ptr is not None and cert is not None and status == "verified"
    assert ptr.version == "v2026.11.01.120000" and ptr.reason == "soaked 2h"
    assert f"sha={sha}" in gh_out.read_text(encoding="utf-8")
