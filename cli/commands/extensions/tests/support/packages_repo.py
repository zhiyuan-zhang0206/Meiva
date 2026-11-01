# meiva:note
# The git side of a package-refresh case: a bare origin plus a working clone under `tmp_path`,
# skills committed into it, and the registry row a refresh pass is about to compare against.
# One definition so each case writes the same repository shape.
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from base.packages.extensions import install_registry as reg
from base.paths import meiva_home, resolve_meiva_home

__all__ = [
    "commit_push",
    "core_repo",
    "git_in",
    "head_of",
    "mirror_installed_hash",
    "row_of",
    "seed_package",
    "write_skill",
]


def home_of() -> Path:
    return resolve_meiva_home()


def git_in(cwd: Path, *args: str, check: bool = True) -> str:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def write_skill(
    repo: Path, name: str, body: str, manifest: dict[str, object] | None = None
) -> None:
    d = repo / "meiva_builtins" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\n{body}", encoding="utf-8"
    )
    if manifest is not None:
        (d / "meiva-plugin.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )


def head_of(repo: Path) -> str:
    return git_in(repo, "rev-parse", "HEAD").strip()


def commit_push(repo: Path, message: str) -> str:
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-qm", message)
    git_in(repo, "push", "-q", "origin", "main")
    return head_of(repo)


@pytest.fixture
def core_repo(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "--initial-branch=main", str(bare)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    git_in(repo, "config", "user.email", "test@example.invalid")
    git_in(repo, "config", "user.name", "test")
    write_skill(repo, "foo", "# v1\n")
    write_skill(repo, "bar", "# v1\n")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-qm", "v1")
    git_in(repo, "remote", "add", "origin", str(bare))
    git_in(repo, "push", "-q", "-u", "origin", "main")
    return repo


def seed_package(
    repo: Path,
    name: str,
    *,
    applied_rev: str | None,
    mode: reg.UpdateMode | None = None,
    last_check_at: str | None = None,
) -> None:
    src = repo / "meiva_builtins" / "skills" / name
    dest = home_of() / "skills" / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*reg.IGNORED_NAMES))
    reg.register(
        reg.InstalledPackage(
            name=name,
            type="skill",
            origin="repo",
            origin_path=str(src),
            content_hash=reg.tree_hash(dest),
            update=reg.UpdateState(applied_rev=applied_rev, mode=mode, last_check_at=last_check_at),
        ),
        home=meiva_home(),
    )


# meiva:note
# Give `name` the row shape `meiva skill install` writes: both edit guards set.
def mirror_installed_hash(name: str) -> None:
    with reg.mutate(home=meiva_home()) as registry:
        row = next(p for p in registry.packages if p.name == name)
        row.installed_hash = row.content_hash


def row_of(name: str) -> reg.InstalledPackage:
    row = reg.get(name, home=meiva_home())
    assert row is not None
    return row
