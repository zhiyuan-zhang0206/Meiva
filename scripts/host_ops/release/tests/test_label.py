from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.host_ops.release import label as release_label


# meiva:note
# Fixed git invocations on a temporary repository; the arguments are test literals, never caller input.
def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603
        ("git", "-C", str(repo), *arguments), check=True, capture_output=True
    )


def _repository(tmp_path: Path, version: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "meiva"\nversion = "{version}"\n', encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", f"version {version}")
    return repo


# meiva:note
# The refusals are named apart because they ask for different fixes: a wrong label is retyped, a tree without a product version is a different problem in the tree.
def test_a_label_equal_to_the_tree_is_allowed() -> None:
    ok, reason = release_label.judge("1.0.0", "1.0.0")

    assert (ok, reason) == (True, "matches-tree")


def test_a_label_the_tree_does_not_carry_is_refused() -> None:
    ok, reason = release_label.judge("1.0.1", "1.0.0")

    assert (ok, reason) == (False, "version-mismatch")


def test_a_label_that_is_not_three_parts_is_refused() -> None:
    for label in ("1.0", "v1.0.0", "1.0.0-rc1", ""):
        ok, reason = release_label.judge(label, "1.0.0")
        assert (ok, reason) == (False, "bad-label"), label


def test_a_tree_without_a_product_version_is_named() -> None:
    ok, reason = release_label.judge("1.0.0", None)

    assert (ok, reason) == (False, "no-tree-version")


# meiva:note
# The reason this reads git instead of the filesystem: the gate accepts a target that may be any commit, while the checkout in the runner is usually a different one. A working-tree read would pass exactly the pairs it must refuse.
def test_the_version_comes_from_the_target_commit_not_the_checkout(tmp_path: Path) -> None:
    repo = _repository(tmp_path, "1.0.0")
    first = subprocess.run(  # noqa: S603
        ("git", "-C", str(repo), "rev-parse", "HEAD"), check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "meiva"\nversion = "2.0.0"\n', encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "version 2.0.0")

    assert release_label.product_version_at(repo, first) == "1.0.0"
    assert release_label.product_version_at(repo, "HEAD") == "2.0.0"


def test_a_target_without_a_pyproject_has_no_tree_version(tmp_path: Path) -> None:
    repo = _repository(tmp_path, "1.0.0")
    _git(repo, "rm", "-q", "pyproject.toml")
    _git(repo, "commit", "-q", "-m", "drop pyproject")

    assert release_label.product_version_at(repo, "HEAD") is None
    assert release_label.product_version_at(repo, "deadbeef") is None
