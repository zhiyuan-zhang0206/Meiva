"""Host version — derived from the checkout's commit, never hand-maintained.

The host version is the date axis of the running build: `YYYY.M.D` from the
commit date of the checkout's HEAD, with `+g<short-sha>` added in the display
form. It always exists on every checkout-mode machine, advances by itself,
and needs no release process and no bump discipline (task #2915 design §5.5
v3 — the hand-maintained `[project].version` was rejected as the gate source;
it survives only as the checkout-less fallback).

The gates compare against the bare `YYYY.M.D` string — `engines.meiva` ranges
via `base.packages.plugins.manifest.range_allows` plus the `requires_commit` ancestor
check. Prefer the display form only where a human reads it (status output,
log lines).

Fallback chain: git-derived date -> `[project].version` from `pyproject.toml`
(the checkout-less / wheel case) -> `HostVersionError` (callers surface
"unknown" rather than guessing a number a gate would trust).
"""

from __future__ import annotations

from pathlib import Path

from base.host.git.gitenv import git_env
from base.host.git.host_version_plan import derived_from_log, display_form, version_from_pyproject
from base.host.proc import SubprocessError, run_bounded

_GIT_TIMEOUT_S = 10.0


# meiva:note
# Repository root derived from this module's path (`base/host/git/host_version.py`).
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


class HostVersionError(RuntimeError):
    """No host version could be derived — not a git checkout and no usable
    `pyproject.toml` version. Callers must not substitute a default."""


def host_version_from_repo(repo: Path) -> str:
    """The Meiva version this checkout carries (`[project].version` in its
    pyproject.toml) — the host version `engines.meiva` is checked against."""
    pyproject = repo / "pyproject.toml"
    try:
        raw = pyproject.read_text(encoding="utf-8")
    except OSError as e:
        raise HostVersionError(f"cannot determine host version: no pyproject.toml in {repo}") from e
    parsed = version_from_pyproject(raw, str(pyproject))
    if parsed.version is None:
        raise HostVersionError(parsed.error)
    return parsed.version


def _derived(repo: Path) -> tuple[str, str] | None:
    """(bare version, short sha) from `repo`'s HEAD commit, or None."""
    try:
        result = run_bounded(  # git + fixed args, no user input
            ["git", "-C", str(repo), "log", "-1", "--format=%cd|%h", "--date=short"],
            capture_output=True,
            text=True,
            env=git_env(),
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, SubprocessError):
        return None
    return derived_from_log(result.returncode, result.stdout)


def host_version(repo: Path | None = None) -> str:
    """The gate-comparable host version: bare `YYYY.M.D`.

    Raises:
        HostVersionError: no git commit and no readable pyproject version.
    """
    repo = repo or _repo_root()
    derived = _derived(repo)
    if derived is not None:
        return derived[0]
    return host_version_from_repo(repo)


def host_version_display(repo: Path | None = None) -> str:
    """The human-facing form: `YYYY.M.D+g<short-sha>` when the checkout is a
    git repo, bare otherwise (falls back through `host_version`)."""
    repo = repo or _repo_root()
    derived = _derived(repo)
    if derived is not None:
        return display_form(derived)
    return host_version(repo)
