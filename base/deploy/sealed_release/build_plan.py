# meiva:note
# How a staged release's Python side is built, as values: the commit time its record carries,
# the environment the build runs in and the two commands, `uv sync` and `compileall`. `stage.py`
# runs them.
# meiva:link future/infra/lifecycle/release-dir-atomic-code-swap.md

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, assert_never

# meiva:note
# An inherited `VIRTUAL_ENV` makes uv sync into that environment instead of the release's own
# (postmortem 0006); `UV_PROJECT_ENVIRONMENT` moves the venv off the path the release embeds.
_SCRUBBED_ENV = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")
_SKIP_COMPILE = r"(^|/)\.venv/"


# meiva:note
# A commit time as Git prints it (`%cI`, with the committer's offset) in UTC, the form a release
# record requires.
def utc_iso(committed: str) -> str:
    return datetime.fromisoformat(committed).astimezone(UTC).isoformat()


def build_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in environ.items() if key not in _SCRUBBED_ENV}


# meiva:note
# Each release file must be its own inode inside the tree. A hardlink into uv's cache would be
# sealed along with the release and move the release's ctime whenever another link changes; a
# symlink would put the file outside the tree, unsealed and unverified. So the link mode is named
# rather than left to uv's default, an inherited `UV_LINK_MODE` or a `uv.toml`: a clone where the
# filesystem shares blocks (macOS), a copy elsewhere.
def sync_argv(uv: str, *, system: Literal["linux", "darwin"]) -> list[str]:
    argv = [uv, "sync", "--frozen", "--compile-bytecode"]
    match system:
        case "darwin":
            return [*argv, "--link-mode=clone"]
        case "linux":
            return [*argv, "--link-mode=copy"]
        case _:
            assert_never(system)


# meiva:note
# A sealed tree cannot take the `__pycache__` an import would write, so every source file is
# compiled before sealing; the venv was already compiled by uv.
def compile_argv(python: Path, release: Path) -> list[str]:
    return [str(python), "-m", "compileall", "-q", "-j", "0", "-x", _SKIP_COMPILE, str(release)]
