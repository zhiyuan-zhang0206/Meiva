# meiva:note
# The shared commit/push hook refuses to run from the production source checkout (task #7417).
# `setup-worktree.sh` already refuses to make a worktree there; this is the layer behind it, for
# a worktree made with a bare `git worktree add` that skipped the script. Exercised as a process
# on throwaway repositories, with HOME pointed at a temporary directory.


from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = ("scripts/hooks/prod-source.sh", "scripts/hooks/refuse-prod-source.sh")


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key != "MEIVA_HOME"
    }
    env.update(
        HOME=str(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="test",
        GIT_AUTHOR_EMAIL="test@meiva",
        GIT_COMMITTER_NAME="test",
        GIT_COMMITTER_EMAIL="test@meiva",
    )
    env.update(extra)
    return env


# meiva:note
# Every git argv here is a fixture literal on a test-owned temporary repository.
def _git(cwd: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, env=_env(home), capture_output=True, text=True, check=False
    )


def _clone(path: Path, home: Path) -> Path:
    path.mkdir(parents=True)
    assert _git(path, home, "init", "-q", "--initial-branch=main").returncode == 0
    for rel in _SCRIPTS:
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(ROOT / rel, path / rel)
    assert _git(path, home, "add", "-A").returncode == 0
    assert _git(path, home, "commit", "-q", "--no-verify", "-m", "seed").returncode == 0
    hook = path / ".git/hooks/pre-commit"
    _ = hook.write_text("#!/usr/bin/env bash\nexec bash scripts/hooks/refuse-prod-source.sh\n")
    hook.chmod(0o755)
    return path


def _guard(cwd: Path, home: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/hooks/refuse-prod-source.sh"],
        cwd=cwd,
        env=_env(home, **extra),
        capture_output=True,
        text=True,
        check=False,
    )


def _commit(cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    _ = (cwd / "change.txt").write_text("change\n")
    assert _git(cwd, home, "add", "change.txt").returncode == 0
    return _git(cwd, home, "commit", "-q", "-m", "change")


def test_a_bare_worktree_of_the_production_checkout_cannot_commit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    prod = _clone(home / ".meiva/source", home)
    worktree = tmp_path / "sneaky"

    added = _git(prod, home, "worktree", "add", "-q", "-b", "sneaky", str(worktree))
    assert added.returncode == 0, added.stderr

    refused = _commit(worktree, home)
    assert refused.returncode != 0
    assert "refusing to commit or push from the production source checkout" in refused.stderr
    assert "cd ~/Meiva && bash scripts/setup-worktree.sh <task>" in refused.stderr
    assert _git(worktree, home, "log", "-1", "--format=%s").stdout.strip() == "seed"

    assert _guard(prod, home).returncode == 1


def test_the_development_clone_and_a_prefix_sibling_commit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _ = _clone(home / ".meiva/source", home)
    for name in ("Meiva", ".meiva/source-dev"):
        dev = _clone(home / name, home)
        worktree = dev / ".worktrees/task"
        assert _git(dev, home, "worktree", "add", "-q", "-b", "task", str(worktree)).returncode == 0
        done = _commit(worktree, home)
        assert done.returncode == 0, done.stderr


def test_symlinks_and_a_custom_meiva_home_are_matched_physically(tmp_path: Path) -> None:
    home = tmp_path / "home"
    real = _clone(tmp_path / "real-prod", home)
    (home / ".meiva").mkdir(parents=True)
    (home / ".meiva/source").symlink_to(real)
    alias = tmp_path / "alias"
    alias.symlink_to(real)

    assert _guard(alias, home).returncode == 1

    custom = tmp_path / "custom-meiva"
    prod = _clone(custom / "source", home)
    assert _guard(prod, home).returncode == 0
    refused = _guard(prod, home, MEIVA_HOME=str(custom))
    assert refused.returncode == 1
    assert str(prod.resolve()) in refused.stderr


type _HookConfig = dict[str, list[dict[str, list[dict[str, object]]]]]


# meiva:note
# Never a stage `git worktree add` or `checkout` triggers (post-checkout and friends): the fleet
# update preflight makes a legitimate read-only worktree of the production checkout.
def test_registered_on_commit_and_push_only() -> None:
    config = cast("_HookConfig", yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text()))
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    guard = hooks["refuse-prod-source"]
    assert guard["entry"] == "bash scripts/hooks/refuse-prod-source.sh"
    assert guard["stages"] == ["pre-commit", "pre-push"]
    assert guard["always_run"] is True
    assert guard["pass_filenames"] is False
