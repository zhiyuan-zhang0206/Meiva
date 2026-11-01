# meiva:note
# Plumbing shared by the pre-push hook tests: a throwaway repository and the hook table.
# Both files that exercise `scripts/provision/prepush_*` and the hook wrappers around them need the
# same two things — a repository that git will operate on without the caller's environment, and the
# `files:` patterns from the checked-in configuration. Reading them here keeps one reader for the
# fact that the configuration is authoritative.

# meiva:note
# Every subprocess call in this module runs git, bash or this interpreter against a throwaway
# repository built from fixture literals, so S603 has no untrusted command to flag.
# ruff: noqa: S603

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]


def init_probe_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)


def init_repo_with_origin_main(path: Path) -> str:
    # meiva:note
    # A repo with a local `origin/main` ref pointing at its first commit; returns that sha.
    init_probe_repo(path)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "base"], check=True)
    base_sha = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(path), "update-ref", "refs/remotes/origin/main", base_sha], check=True
    )
    return base_sha


def hook_table() -> dict[str, dict[str, Any]]:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    return {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
