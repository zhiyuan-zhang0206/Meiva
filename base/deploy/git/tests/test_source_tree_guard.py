"""Regression guards for the source-tree integrity detector."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from base.deploy.git import source_tree_guard as stg
from base.deploy.sealed_release import sealed_tree
from base.host.proc import run_bounded
from base.host.system.tests.fake_commands import fake_host

# meiva:note
# The guard runs real git, so the host carries the production runner; the platform and the
# environment are the ones this file names, never the machine's.
_HOST = fake_host(commands=run_bounded)


def _git(source: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_source(source: Path) -> Path:
    """Create a real git repo at `source` with one commit on `main`; returns
    the checkout path."""
    source.mkdir(parents=True)
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.email", "t@t")
    _git(source, "config", "user.name", "t")
    (source / "tracked.txt").write_text("x")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "init")
    return source


def _commit(source: Path, content: str, msg: str) -> str:
    """Add a commit on the current branch from `content`, returning its sha."""
    (source / "tracked.txt").write_text(content)
    _git(source, "add", ".")
    _git(source, "commit", "-m", msg)
    return _git(source, "rev-parse", "HEAD")


# --- detector ---


def test_clean_checkout_reports_no_violations(tmp_path: Path) -> None:
    repo = _init_source(tmp_path / "source")

    assert stg.source_tree_violations(repo, host=_HOST) == ()


def test_untracked_file_outside_whitelist_is_a_violation(tmp_path: Path) -> None:
    repo = _init_source(tmp_path / "source")
    (repo / "junk.txt").write_text("j")

    violations = stg.source_tree_violations(repo, host=_HOST)

    assert any("junk.txt" in v for v in violations)


def test_untracked_dir_outside_whitelist_is_a_violation(tmp_path: Path) -> None:
    repo = _init_source(tmp_path / "source")
    (repo / "junkdir").mkdir()
    (repo / "junkdir" / "inner.txt").write_text("i")

    violations = stg.source_tree_violations(repo, host=_HOST)

    assert any("junkdir" in v for v in violations)


def test_whitelisted_runtime_artifacts_are_not_violations(tmp_path: Path) -> None:
    """The built frontend bundle (frontend/) is the one legal untracked tree."""
    repo = _init_source(tmp_path / "source")
    (repo / "frontend" / ".next").mkdir(parents=True)
    (repo / "frontend" / ".next" / "build.txt").write_text("b")
    (repo / "frontend" / "tsconfig.tsbuildinfo").write_text("t")

    assert stg.source_tree_violations(repo, host=_HOST) == ()


def test_gitignored_paths_are_not_violations(tmp_path: Path) -> None:
    """Gitignored runtime paths need no whitelist entry: the detector never
    sees them (the documented contract — e.g. the accounting ledger at
    ``scripts/ci/ci_usage/``)."""
    repo = _init_source(tmp_path / "source")
    (repo / ".gitignore").write_text("scripts/ci/ci_usage/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore ci usage ledger")
    (repo / "scripts" / "ci" / "ci_usage").mkdir(parents=True)
    (repo / "scripts" / "ci" / "ci_usage" / "ledger.jsonl").write_text("{}\n")

    assert stg.source_tree_violations(repo, host=_HOST) == ()


def test_tracked_modification_is_a_violation(tmp_path: Path) -> None:
    repo = _init_source(tmp_path / "source")
    (repo / "tracked.txt").write_text("changed")

    violations = stg.source_tree_violations(repo, host=_HOST)

    assert any("tracked change" in v and "tracked.txt" in v for v in violations)


def test_head_move_alone_is_not_a_violation(tmp_path: Path) -> None:
    """A clean checkout whose HEAD moved is not tampering. No lifecycle records
    an installed commit for a source checkout, so a legitimate checkout move
    must not become a permanent alert."""
    repo = _init_source(tmp_path / "source")
    _commit(repo, "y", "c2")

    assert stg.source_tree_violations(repo, host=_HOST) == ()


def test_non_git_checkout_signals_guard_skipped(tmp_path: Path) -> None:
    """A checkout that is not a git repo must not look clean: the guard
    cannot see anything, so it reports itself as skipped (not tampered)."""
    repo = tmp_path / "plain"
    repo.mkdir()

    assert stg.source_tree_violations(repo, host=_HOST) == ("guard skipped: not a git checkout",)


def test_git_unavailable_signals_guard_skipped(tmp_path: Path) -> None:
    """A broken git (binary missing / command error / timeout) makes the
    detector blind — the probe must alert on the blind guard, never pass it."""
    repo = _init_source(tmp_path / "source")
    assert stg.git(repo, "-c", "alias.hang=!sleep 10", "hang", host=_HOST, timeout=0.05) is None
    (repo / ".git" / "HEAD").write_text("corrupt\n")

    assert stg.source_tree_violations(repo, host=_HOST) == ("guard skipped: git unavailable",)


def test_git_command_failure_signals_guard_skipped(tmp_path: Path) -> None:
    """A git command that runs but exits non-zero is a blind guard too."""
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /nonexistent\n")

    assert stg.source_tree_violations(repo, host=_HOST) == ("guard skipped: git unavailable",)


def _release_source(home: Path) -> Path:
    release = home / "source-releases" / "1-0123456789ab"
    (release / "pkg").mkdir(parents=True)
    (release / "pkg" / "mod.py").write_text("VALUE = 1\n")
    sealed_tree.write_manifest(release, sealed_tree.snapshot(release), host=_HOST)
    (home / "source").symlink_to("source-releases/1-0123456789ab")
    return home / "source"


# meiva:note
# A pointer into the home's releases runs a release, which has no `.git`; its manifest is what the
# guard compares, so an added file is found without Git and an unchanged release reads clean.
def test_a_release_pointer_is_judged_by_its_manifest(tmp_path: Path) -> None:
    source = _release_source(tmp_path)
    assert stg.source_tree_violations(source, host=_HOST) == ()

    (source / "pkg" / "extra.py").write_text("")

    assert "added: pkg/extra.py" in stg.source_tree_violations(source, host=_HOST)


def test_a_release_the_guard_cannot_verify_is_a_blind_guard(tmp_path: Path) -> None:
    (tmp_path / "source").symlink_to("source-releases/1-0123456789ab")
    (finding,) = stg.source_tree_violations(tmp_path / "source", host=_HOST)
    assert finding.startswith("guard skipped: cannot verify the release")

    (tmp_path / "source-releases" / "1-0123456789ab").mkdir(parents=True)
    (finding,) = stg.source_tree_violations(tmp_path / "source", host=_HOST)
    assert finding.startswith("guard skipped: cannot verify the release")


# meiva:note
# Release mode follows the pointer, not a file inside the tree: a checkout holding a stray release
# record, or a release-shaped manifest beside it, is still judged by Git.
def test_a_stray_release_record_does_not_take_a_checkout_off_git(tmp_path: Path) -> None:
    repo = _init_source(tmp_path / "source")
    (repo / ".meiva-release.json").write_text("{}")
    (repo / "tracked.txt").write_text("edited")

    findings = stg.source_tree_violations(repo, host=_HOST)

    assert "untracked outside whitelist: .meiva-release.json" in findings
    assert any(line.startswith("tracked change:") for line in findings)


# --- whitelist self-check ---


def test_whitelist_validation_rejects_empty() -> None:
    """An empty whitelist would flag every untracked file, the runtime artifacts
    included, as tamper — the guard must fail fast instead."""
    with pytest.raises(ValueError, match="must be non-empty"):
        stg.validate_whitelist(())


def test_whitelist_validation_rejects_catch_all_patterns() -> None:
    """A catch-all pattern whitelists arbitrary paths — detection would be
    always empty (the silent-no-op misconfiguration)."""
    for bad in (("*",), ("**",), ("*/*",), ("frontend/", "*")):
        with pytest.raises(ValueError, match="arbitrary paths"):
            stg.validate_whitelist(bad)


def test_whitelist_validation_accepts_real_whitelist() -> None:
    """The shipped whitelist stays sane: non-empty, no catch-all. Guards the
    constant against a future edit that silently silences the guard."""
    stg.validate_whitelist(stg.SOURCE_TREE_WHITELIST)


def test_whitelist_matches_only_runtime_artifacts() -> None:
    """Direct invariant for 'the whitelist must not swallow everything': a
    junk path is never whitelisted while the frontend bundle stays legal."""
    assert stg.is_whitelisted("junk.txt") is False
    assert stg.is_whitelisted("junkdir/inner.txt") is False
    assert stg.is_whitelisted("frontend/.next/build.txt") is True
    assert stg.is_whitelisted("frontend/") is True
