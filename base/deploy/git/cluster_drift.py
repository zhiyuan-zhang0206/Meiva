# meiva:note
# Prod-source git introspection — the checkout a source-run home executes.
#
# The prod source (`$MEIVA_HOME/source`) is the tree a source-run home's services
# run out of. Facts about it that status surfaces show:
#
# - `prod_source_head_sha(home)` — its HEAD commit, reported per host in the roster
#   beside the commit the answering process loaded (`base.native_process.loaded_commit`).
# - `checkout_head_sha(repo)` — the same read for an explicit checkout.
# - `prod_source_branch_drift(home)` — its current branch when it is not `main`, i.e.
#   an agent developed *in* the prod tree instead of a worktree (un-reviewed code
#   on the running host).
#
# All are local, read-only git subprocess calls against a fixed path, with no
# dependency on the CLI or gateway layers, so the gateway roster and the ops
# status probe can share them with `meiva status`.

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from base.host.git.gitenv import git_env
from base.host.proc import run_bounded

# These reads are local-only (rev-parse / merge-base against an existing
# checkout), so a low ceiling is right — a status render waits on them. Bounded
# via `run_bounded` all the same: a plain `subprocess.run` timeout kills only the
# direct child and leaves its descendants behind on every expiry, and a status
# probe runs often enough to accumulate them.
_GIT_TIMEOUT_S = 5.0


# meiva:note
# The installed source checkout of the unit at `home`: `<home>/source`.
#
# Every unit runs from its own `<home>/source` checkout (source mode), so the
# home names its checkout; a home without one (a test home) has no such directory.
def prod_source_dir(home: Path) -> Path:
    return home / "source"


# meiva:note
# Run a read-only git command in the checkout `repo`, returning trimmed stdout.
#
# The health preflight checks the checkout a start is running FROM, which is the
# prod source on a prod install but a worktree elsewhere. Returns None when the
# checkout is absent / not a git repo / git is unavailable / the command fails —
# every caller treats "cannot read" as "nothing to report" rather than an error.
def _git_ro(repo: Path, *args: str, environ: Mapping[str, str]) -> str | None:
    if not (repo / ".git").exists():
        return None
    try:
        result = run_bounded(  # git + fixed path + literal args, no user input
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            env=git_env(environ),
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# meiva:note
# Whether the calling process loaded its code from the prod source checkout of `home`.
#
# Every "checkout vs running code" comparison needs this: the checkout facts
# (`prod_source_head_sha`) are read from the installed prod source, while
# `base.native_process.loaded_commit` reports the tree the *process* was loaded from. On prod
# those are the same tree and the comparison is meaningful; in a dev worktree
# they are two different checkouts, so a difference says nothing about drift.
# Returns False when the prod source cannot be resolved — unknown layout, so no
# comparison is licensed.
def running_from_prod_source(home: Path) -> bool:
    try:
        # base/deploy/git/cluster_drift.py -> the checkout root.
        return Path(__file__).resolve().parents[3] == prod_source_dir(home).resolve()
    except OSError:
        return False


def prod_source_head_sha(home: Path, *, environ: Mapping[str, str]) -> str | None:
    """The prod source's current HEAD commit sha, or None if it cannot be read."""
    return _git_ro(prod_source_dir(home), "rev-parse", "HEAD", environ=environ)


def checkout_head_sha(repo: Path, *, environ: Mapping[str, str]) -> str | None:
    """`repo`'s current HEAD commit sha, or None if it cannot be read."""
    return _git_ro(repo, "rev-parse", "HEAD", environ=environ)


def prod_source_branch_drift(home: Path, *, environ: Mapping[str, str]) -> str | None:
    """The prod source's current branch when it has drifted off `main`, else None.

    The prod source must sit on reviewed `main`. A non-`main` branch means an
    agent developed in the prod tree (a `git checkout -b` there instead of a
    `git worktree`), putting un-reviewed code on the running host. A detached
    HEAD is the source-mode release state — fleet_update materializes every
    checkout as `--detach NEW` — so consumers treat it as the designed steady
    state, not drift, and report it as the literal branch `"HEAD"`.
    """
    branch = _git_ro(prod_source_dir(home), "rev-parse", "--abbrev-ref", "HEAD", environ=environ)
    return branch if branch and branch != "main" else None
