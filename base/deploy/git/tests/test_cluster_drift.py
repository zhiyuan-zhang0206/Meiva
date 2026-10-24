"""Tests for base.deploy.git.cluster_drift — prod-source git introspection."""

from __future__ import annotations

import subprocess
from pathlib import Path

from base.deploy.git.cluster_drift import (
    checkout_head_sha,
    prod_source_branch_drift,
    prod_source_dir,
    prod_source_head_sha,
    running_from_prod_source,
)


def _git(source: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_prod_source(source: Path, *, branch: str = "main") -> str:
    """Create a real git repo at `source` with one commit on `main`; optionally
    leave it checked out on a different branch. Returns the HEAD sha."""
    source.mkdir(parents=True)
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "t@t")
    _git(source, "config", "user.name", "t")
    (source / "f").write_text("x")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "init")
    if branch != "main":
        _git(source, "checkout", "-b", branch)
    return _git(source, "rev-parse", "HEAD")


def _commit(source: Path, content: str, msg: str) -> str:
    """Add a commit on the current branch from `content`, returning its sha."""
    (source / "f").write_text(content)
    _git(source, "add", ".")
    _git(source, "commit", "-m", msg)
    return _git(source, "rev-parse", "HEAD")


def test_head_sha_absent(tmp_path: Path) -> None:
    """No source repo → None (nothing to read)."""
    assert prod_source_head_sha(tmp_path, environ={}) is None


def test_head_sha_returns_head(tmp_path: Path) -> None:
    sha = _init_prod_source(tmp_path / "source")
    assert prod_source_head_sha(tmp_path, environ={}) == sha


def test_branch_drift_absent(tmp_path: Path) -> None:
    assert prod_source_branch_drift(tmp_path, environ={}) is None


def test_branch_drift_on_main(tmp_path: Path) -> None:
    _init_prod_source(tmp_path / "source")
    assert prod_source_branch_drift(tmp_path, environ={}) is None


def test_branch_drift_feature_branch(tmp_path: Path) -> None:
    _init_prod_source(tmp_path / "source", branch="meiva-7/fix")
    assert prod_source_branch_drift(tmp_path, environ={}) == "meiva-7/fix"


# meiva:note
# The home names its checkout: `<home>/source`. A link at `<home>/meiva`
# or the host's bare `meiva` names no other.
def test_prod_source_dir_is_the_homes_own_source(tmp_path: Path) -> None:
    home = tmp_path / "meivahome"
    home.mkdir()
    elsewhere = tmp_path / "opt" / "meiva" / "source" / ".venv" / "bin" / "meiva"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("#!/bin/sh\n")
    (home / "meiva").symlink_to(elsewhere)
    assert prod_source_dir(home) == home / "source"


def test_checkout_head_sha_reads_an_explicit_checkout(tmp_path: Path) -> None:
    sha = _init_prod_source(tmp_path / "wt")
    assert checkout_head_sha(tmp_path / "wt", environ={}) == sha
    assert checkout_head_sha(tmp_path / "absent", environ={}) is None


def test_running_from_prod_source_recognizes_the_loaded_checkout(tmp_path: Path) -> None:
    """The module anchors on the checkout it was imported from, not on its package depth."""
    checkout = Path(__file__).resolve().parents[4]
    loaded_home = tmp_path / "loaded"
    loaded_home.mkdir()
    (loaded_home / "source").symlink_to(checkout)
    assert running_from_prod_source(loaded_home) is True
    other = tmp_path / "other"
    (other / "source").mkdir(parents=True)
    assert running_from_prod_source(other) is False
