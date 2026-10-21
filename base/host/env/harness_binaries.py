# meiva:note
# Where an external harness CLI lives, and the PATH its child process runs with.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

# meiva:note
# The directories a harness CLI is installed into outside PATH: user-local installs and
# the two system prefixes. A harness child cannot rely on PATH alone — the runner that
# starts it hands the bridge an allowlisted environment with no PATH at all, so a bare
# name resolves against the OS default (/usr/bin:/bin:/usr/sbin:/sbin) and every
# external harness failed to start from a real spawn while `which codex` in a shell
# worked (#5491). The launchd job that starts the runner sees the same gap.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
HARNESS_BIN_DIRS = (
    ".local/bin",
    ".npm-global/bin",
    ".bun/bin",
    ".claude/local",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)

_SYSTEM_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


# meiva:note
# A miss, carrying the PATH and every directory that was searched: the bare ENOENT from
# an exec names only the program, which is what made the first real spawn hard to read.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
class HarnessBinaryNotFoundError(FileNotFoundError):
    pass


# meiva:note
# `HARNESS_BIN_DIRS` (or a caller's list) with the relative entries anchored at `home`.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def harness_bin_dirs(
    home: Path | None = None, bin_dirs: Sequence[str] | None = None
) -> tuple[str, ...]:
    entries = HARNESS_BIN_DIRS if bin_dirs is None else bin_dirs
    root = Path.home() if home is None else home
    return tuple(entry if Path(entry).is_absolute() else str(root / entry) for entry in entries)


# meiva:note
# `(path, on_path)` for `program`; `on_path` says PATH itself resolved it. A CLI installed
# as a symlink (Homebrew's `codex` points at its npm entry) comes back as the executable
# found, not its target: the target is a script the kernel may not be able to exec.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def locate_harness_binary(
    program: str,
    *,
    home: Path | None = None,
    path_env: str | None = None,
    bin_dirs: Sequence[str] | None = None,
) -> tuple[str | None, bool]:
    found = shutil.which(program, path=path_env)
    if found is not None:
        return found, True
    for entry in harness_bin_dirs(home, bin_dirs):
        candidate = Path(entry) / program
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate), False
    return None, False


# meiva:note
# Absolute path of the CLI a harness session runs, or a report of what was tried. An
# explicit path (one carrying a separator) is used as given: a deployment that pins the
# binary in configuration must not be steered onto another install.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def resolve_harness_binary(
    program: str,
    *,
    home: Path | None = None,
    path_env: str | None = None,
    bin_dirs: Sequence[str] | None = None,
) -> str:
    if os.sep in program:
        if Path(program).is_file() and os.access(program, os.X_OK):
            return program
        raise HarnessBinaryNotFoundError(f"{program} is not an executable file")
    found, _ = locate_harness_binary(program, home=home, path_env=path_env, bin_dirs=bin_dirs)
    if found is None:
        searched = ", ".join(harness_bin_dirs(home, bin_dirs))
        raise HarnessBinaryNotFoundError(
            f"{program} was not found on PATH ({path_env or 'unset'}) or in {searched}"
        )
    return found


# meiva:note
# PATH for a harness child: what the host has, then every known install dir, then the
# system dirs, de-duplicated in that order.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def harness_child_path(*, path_env: str | None = None, home: Path | None = None) -> str:
    entries = [
        *(path_env or "").split(os.pathsep),
        *harness_bin_dirs(home),
        *_SYSTEM_BIN_DIRS,
    ]
    return os.pathsep.join(dict.fromkeys(entry for entry in entries if entry))


# meiva:note
# `environ` plus a PATH the harness and its own children can resolve through. The
# allowlisted child environment deliberately carries no PATH; a session delivery point is
# where it is rebuilt, the same rule the daemon's venv activation follows.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def with_harness_path(environ: Mapping[str, str], *, home: Path | None = None) -> dict[str, str]:
    return {**environ, "PATH": harness_child_path(path_env=environ.get("PATH"), home=home)}
