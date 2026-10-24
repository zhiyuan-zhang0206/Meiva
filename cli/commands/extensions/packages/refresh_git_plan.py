# meiva:note
# Network Git commands for a refresh pass, assembled from values. The shell reads the current
# timeout and bounded Git environment, then runs this plan through run_bounded.

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitCommand:
    argv: tuple[str, ...]
    cwd: Path | None
    env: Mapping[str, str]
    timeout: float


def core_listing(
    repo: Path, remote: str, ref: str, *, env: Mapping[str, str], timeout: float
) -> GitCommand:
    return GitCommand(("git", "ls-remote", remote, ref), repo, dict(env), timeout)


def core_fetch(
    repo: Path, remote: str, ref: str, *, env: Mapping[str, str], timeout: float
) -> GitCommand:
    return GitCommand(("git", "fetch", "--no-tags", remote, ref), repo, dict(env), timeout)


def package_listing(
    source: str, ref: str | None, *, env: Mapping[str, str], timeout: float
) -> GitCommand:
    return GitCommand(("git", "ls-remote", source, ref or "HEAD"), None, dict(env), timeout)
