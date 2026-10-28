# meiva:note
# What the host version is made of, as values: the three-segment version grammar every gate compares
# (the host's own and a plugin manifest's), how the `[project].version` of a pyproject text is read,
# how the commit date and short sha of `git log` output become the derived version and how a derived
# version is shown. `host_version.py` reads the checkout and runs git.

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import total_ordering
from typing import Any

# meiva:note
# The three-segment version every gate compares: the host's own and a plugin manifest's
# (`base.packages.plugins.manifest` builds its ranges on it).

_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?$")
_PRE_RE = re.compile(r"^(dev|alpha|a|beta|b|preview|pre|rc|c)(?:[.-]?(\d+))?$", re.I)
_PRE_ALIASES = {"alpha": "a", "beta": "b", "preview": "rc", "pre": "rc", "c": "rc"}
_PRE_ORDER = {"dev": 0, "a": 1, "b": 2, "rc": 3}


def _normalize_prerelease(raw: str) -> str:
    match = _PRE_RE.match(raw)
    if match is None:
        return raw.lower()
    label = _PRE_ALIASES.get(match[1].lower(), match[1].lower())
    return f"{label}.{int(match[2] or 0)}"


def _prerelease_key(raw: str) -> tuple[int, int, str]:
    match = re.fullmatch(r"(dev|a|b|rc)\.(\d+)", raw)
    if match is not None:
        return (_PRE_ORDER[match[1]], int(match[2]), "")
    return (4, 0, raw)


# meiva:note
# Three-segment version; prereleases sort below the matching release.
@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int = 0
    patch: int = 0
    prerelease: str | None = None

    def __lt__(self, other: Version) -> bool:
        left = (self.major, self.minor, self.patch)
        right = (other.major, other.minor, other.patch)
        if left != right:
            return left < right
        if self.prerelease is None:
            return False
        if other.prerelease is None:
            return True
        return _prerelease_key(self.prerelease) < _prerelease_key(other.prerelease)


def parse_version(raw: str) -> Version | None:
    m = _VERSION_RE.match(raw.strip())
    if m is None:
        return None
    prerelease = _normalize_prerelease(m[4]) if m[4] is not None else None
    return Version(int(m[1]), int(m[2] or 0), int(m[3] or 0), prerelease)


# meiva:note
# No host version could be read from a pyproject: its text says why.
class PyprojectVersionError(ValueError):
    pass


_NO_VERSION = "cannot determine host version: "


# meiva:note
# The refusal for a checkout whose pyproject.toml cannot be read.
def unreadable_pyproject_message(repo: str) -> str:
    return f"{_NO_VERSION}no pyproject.toml in {repo}"


# meiva:note
# Read `[project].version` from the text of the pyproject at `pyproject_path`. Text that is not TOML
# or a version that is not a three-segment version raises `PyprojectVersionError` naming what is
# wrong.
def version_from_pyproject(raw: str, pyproject_path: str) -> str:
    try:
        data: dict[str, Any] = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise PyprojectVersionError(f"{_NO_VERSION}{e}") from e
    project: dict[str, Any] | None = data.get("project")
    version = project.get("version") if project is not None else None
    if not isinstance(version, str) or parse_version(version) is None:
        raise PyprojectVersionError(f"{_NO_VERSION}{pyproject_path} [project].version invalid")
    return version


# meiva:note
# The (bare version, short sha) a `git log -1 --format=%cd|%h --date=short` run printed, or None
# when git failed or its output is not a date and a sha.
def derived_from_log(returncode: int, stdout: str) -> tuple[str, str] | None:
    if returncode != 0:
        return None
    date_part, _, sha = stdout.strip().partition("|")
    try:
        year, month, day = date_part.split("-")
        return f"{int(year)}.{int(month)}.{int(day)}", sha
    except ValueError:
        return None


# meiva:note
# The human-facing form of a derived version: `YYYY.M.D+g<short-sha>`.
def display_form(derived: tuple[str, str]) -> str:
    return f"{derived[0]}+g{derived[1]}"
