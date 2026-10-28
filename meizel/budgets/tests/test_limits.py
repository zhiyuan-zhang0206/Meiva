"""The file ceiling and the directory cap, evaluated against a temporary Git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meizel.budgets import directory_budget, limits

_STRUCTURE = ("base", "tests")


def _repo(root: Path, files: dict[str, int]) -> Path:
    for name, lines in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n" * lines, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)  # noqa: S603
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)  # noqa: S603
    return root


@pytest.mark.parametrize(("lines", "flagged"), [(800, False), (801, True)])
def test_a_file_over_the_ceiling_is_flagged(tmp_path: Path, lines: int, flagged: bool) -> None:
    repo = _repo(tmp_path, {"base/big.py": lines})
    errors = limits.check([repo / "base"], [repo], repo, _STRUCTURE)
    assert bool(errors) is flagged
    if flagged:
        [error] = errors
        assert error.startswith(
            f"base/big.py:{lines}: file is {lines} lines, over the 800-line hard ceiling — "
        )
        assert "cohesive subpackage" in error
        assert "pure decisions from I/O" in error


@pytest.mark.parametrize(("entries", "flagged"), [(20, False), (21, True)])
def test_a_directory_over_the_cap_is_flagged(tmp_path: Path, entries: int, flagged: bool) -> None:
    repo = _repo(tmp_path, {f"base/pkg/m{i}.py": 1 for i in range(entries)})
    errors = limits.check([repo / "base"], [repo], repo, _STRUCTURE)
    assert bool(errors) is flagged
    if flagged:
        [error] = errors
        assert error.startswith(
            f"base/pkg: directory has {entries} direct entries, over the 20-entry cap — "
        )
        assert "directory depth is unrestricted" in error
        assert "cohesive subpackages" in error


def test_the_file_scope_skips_hidden_cache_and_migration_directories(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {"base/ok.py": 1, "base/.hidden/big.py": 900, "base/migrations/big.py": 900},
    )
    assert limits.budget_files([repo / "base"], repo, _STRUCTURE) == {repo / "base" / "ok.py"}


def test_only_a_migrations_directory_is_skipped_not_a_file_of_that_name(tmp_path: Path) -> None:
    (tmp_path / "migrations").write_text("x = 1\n", encoding="utf-8")
    nested = tmp_path / "nested"
    (nested / "migrations").mkdir(parents=True)
    (nested / "keep.py").write_text("x = 1\n", encoding="utf-8")
    assert tmp_path / "migrations" in directory_budget.entries(tmp_path)
    assert directory_budget.entries(nested) == [nested / "keep.py"]


def test_the_file_scope_takes_a_python_file_target_and_ignores_other_files(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"base/ok.py": 1, "base/notes.txt": 1})
    targets = [repo / "base" / "ok.py", repo / "base" / "notes.txt"]
    assert limits.budget_files(targets, repo, _STRUCTURE) == {repo / "base" / "ok.py"}
