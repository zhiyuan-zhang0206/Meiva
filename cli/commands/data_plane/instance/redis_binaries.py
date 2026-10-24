# meiva:note
# Which Redis binaries this cluster runs.
#
# A configured override directory wins and must hold both tools; macOS resolves the pinned
# Homebrew formula; elsewhere the bare name resolves through the child's PATH. The shell checks
# the override's files and asks Homebrew for the formula prefix.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BREW_REDIS_FORMULA",
    "BrewFormula",
    "OnPath",
    "OverrideDir",
    "RedisSource",
    "redis_binary_source",
]

BREW_REDIS_FORMULA = "redis@8.2"


# meiva:note
# One home-owned pair under `path`; an incomplete override never falls back to PATH.
@dataclass(frozen=True)
class OverrideDir:
    path: Path


# meiva:note
# The pinned Homebrew formula's `bin` directory.
@dataclass(frozen=True)
class BrewFormula:
    formula: str


# meiva:note
# The bare tool name, resolved through the child's PATH.
@dataclass(frozen=True)
class OnPath: ...


RedisSource = OverrideDir | BrewFormula | OnPath


# meiva:note
# Where this cluster's Redis tools come from.
def redis_binary_source(*, redis_bin_dir: str, macos: bool) -> RedisSource:
    if redis_bin_dir:
        return OverrideDir(Path(redis_bin_dir))
    return BrewFormula(BREW_REDIS_FORMULA) if macos else OnPath()
