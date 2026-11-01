from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.host_ops.release import bisect_train, certify


def _git(repo: Path, *arguments: str) -> str:
    done = subprocess.run(  # noqa: S603
        ("git", "-C", str(repo), *arguments), check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _train_repository(tmp_path: Path) -> tuple[Path, list[str]]:
    repo = tmp_path / "train"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")

    shas: list[str] = []
    for idx in range(6):
        broken = idx >= 3
        value = 99 if broken else 2
        (repo / "calc.py").write_text(
            f"REV = {idx}\n\n\ndef add_one(x: int) -> int:\n    return x + {value - 1}\n",
            encoding="utf-8",
        )
        (repo / "test_calc.py").write_text(
            "from calc import add_one\n\n\ndef test_add_one() -> None:\n    assert add_one(1) == 2\n",
            encoding="utf-8",
        )
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"commit-{idx}")
        shas.append(_git(repo, "rev-parse", "HEAD"))

    assert (
        certify.main(
            [
                "mint",
                "--repo",
                str(repo),
                "--sha",
                shas[0],
                "--source-ref",
                "refs/heads/main",
                "--certified-at",
                "2026-11-01T10:00:00Z",
                "--gate-run-id",
                "1",
                "--gate-run-url",
                "https://github.com/o/r/actions/runs/1",
            ]
        )
        == 0
    )
    return repo, shas


def test_map_pytest_exit_code_treats_collection_and_internal_errors_as_bisect_skip() -> None:
    assert bisect_train.map_pytest_exit_code(0) == 0
    assert bisect_train.map_pytest_exit_code(1) == 1
    for code in (2, 3, 4, 5):
        assert bisect_train.map_pytest_exit_code(code) == 125


def test_bisect_train_plans_and_isolates_exact_culprit_without_mutating_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, shas = _train_repository(tmp_path)
    capsys.readouterr()
    before_head = _git(repo, "rev-parse", "HEAD")

    assert (
        bisect_train.main(
            [
                "plan",
                "--repo",
                str(repo),
                "--good",
                "channel/latest",
                "--bad",
                "HEAD",
                "--pytest-target",
                "test_calc.py::test_add_one",
            ]
        )
        == 0
    )
    planned = json.loads(capsys.readouterr().out)
    assert planned["ok"] is True
    assert planned["good_sha"] == shas[0] and planned["bad_sha"] == shas[-1]
    assert planned["commit_span"] == 5 and planned["max_steps"] == 3

    assert (
        bisect_train.main(
            [
                "run",
                "--repo",
                str(repo),
                "--good",
                "channel/latest",
                "--bad",
                "HEAD",
                "--pytest-target",
                "test_calc.py::test_add_one",
                "--pytest-python",
                sys.executable,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["culprit_sha"] == shas[3]
    assert result["culprit_subject"] == "commit-3"
    assert _git(repo, "rev-parse", "HEAD") == before_head
    assert _git(repo, "status", "--porcelain") == ""


def test_bisect_train_refuses_uncertified_good_without_explicit_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, shas = _train_repository(tmp_path)
    capsys.readouterr()

    assert (
        bisect_train.main(
            [
                "plan",
                "--repo",
                str(repo),
                "--good",
                shas[1],
                "--bad",
                shas[-1],
                "--pytest-target",
                "test_calc.py::test_add_one",
            ]
        )
        == 2
    )
    denied = json.loads(capsys.readouterr().out)
    assert denied["ok"] is False and "not certified" in denied["error"]

    assert (
        bisect_train.main(
            [
                "plan",
                "--repo",
                str(repo),
                "--good",
                shas[1],
                "--bad",
                shas[-1],
                "--pytest-target",
                "test_calc.py::test_add_one",
                "--allow-uncertified-good",
            ]
        )
        == 0
    )
    allowed = json.loads(capsys.readouterr().out)
    assert allowed["ok"] is True and allowed["good_sha"] == shas[1]
