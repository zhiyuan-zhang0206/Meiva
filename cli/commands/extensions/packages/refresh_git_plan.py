# meiva:note
# Network Git commands for a refresh pass, assembled from values. The shell reads the current
# timeout and bounded Git environment, then runs this plan through run_bounded.

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True)
class GitCommand:
    argv: tuple[str, ...]
    cwd: Path | None
    env: Mapping[str, str]
    timeout: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


def core_listing(
    repo: Path, remote: str, ref: str, *, env: Mapping[str, str], timeout: float
) -> GitCommand:
    return GitCommand(("git", "ls-remote", remote, ref), repo, env, timeout)


def core_fetch(
    repo: Path, remote: str, ref: str, *, env: Mapping[str, str], timeout: float
) -> GitCommand:
    return GitCommand(("git", "fetch", "--no-tags", remote, ref), repo, env, timeout)


def package_listing(
    source: str, ref: str | None, *, env: Mapping[str, str], timeout: float
) -> GitCommand:
    return GitCommand(("git", "ls-remote", source, ref or "HEAD"), None, env, timeout)
