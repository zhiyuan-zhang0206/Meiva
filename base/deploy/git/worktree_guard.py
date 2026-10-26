"""Live-anchor scan that guards `git worktree remove` (issue #194).

A worktree removal is silent about what it kills: any live session or process
whose cwd (or interpreter) lies under the worktree loses its floor the moment
the checkout's `.venv` disappears. The prod instance (issue #194): schedule 1
was running from `~/.meiva/.worktrees/u1a-dark-tokens` on a runner host — the
gateway that launched it had been started from the worktree, so the schedule
session anchored there; routine post-merge cleanup would have killed a
production schedule with no warning and no obviously-broken signal afterwards
(the runner's DB row kept saying `running`).

`find_live_anchors` reports the two surfaces that can be checked cheaply:

- PTY sessions, as the pty-sessions service lists them: the cwd each was started
  in — exactly the surface from the incident, since every cluster-owned session
  (schedules, agent shells) records its cwd there.
- Live processes whose cwd / executable / command line is anchored under the
  path (psutil), excluding the invoking job tree — the caller's process chain
  and process group (issue #3685: cleanup normally runs from inside the
  target, `cd <worktree> && check … | tail`).

The guard lives in `scripts/host_ops/check_worktree_remove.py` as the step the
`ship-a-change` skill runs before `git worktree remove`; git itself offers no
hook for that operation, so the check has to sit in the tooling.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

import psutil

from base.sessions.pty import client
from base.sessions.pty.paths import service_socket_path


def _is_obj_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _same_dir(candidate: Path, target: Path) -> bool:
    """Inode-level sameness; a stat failure on either side reads as not-equal."""
    try:
        return candidate.samefile(target)
    except OSError:
        return False


def _folds_case(target: Path) -> bool:
    """Whether the filesystem resolves a case-flipped spelling of `target`.

    macOS APFS folds case; ext4 does not. Probed by flipping one letter at a
    time until a spelling resolves to `target` itself (a path can cross a
    case-sensitive mount boundary). A target that
    does not exist reports False — the string fallback then stays exact.
    """
    text = str(target)
    for i, ch in enumerate(text):
        if not ch.isalpha():
            continue
        try:
            if Path(text[:i] + ch.swapcase() + text[i + 1 :]).samefile(target):
                return True
        except OSError:
            continue
    return False


def _under(path: str | None, target: Path, *, fold: bool) -> bool:
    """Whether `path` lies at or under `target`, spelling-robust.

    A resolved-string prefix alone is spelling-sensitive: `worktree.sh`
    derives its target from bash's logical `pwd` while psutil / the pty
    records report the physical spelling, and on a case-insensitive filesystem
    `.../meiva/...` and `.../Meiva/...` name the same directory — the miss read as
    "no anchor" (fail-open, task #3707 QA). Walk the candidate's ancestors and
    compare inodes instead; a candidate that cannot be stat'ed (deleted, not
    materialized) falls back to the resolved-string compare — casefolded
    wholesale when the filesystem folds case (`fold`).
    """
    if not path:
        return False
    try:
        candidate = Path(path).resolve()
    except OSError:
        return False
    if any(_same_dir(ancestor, target) for ancestor in (candidate, *candidate.parents)):
        return True
    left, right = str(candidate), str(target)
    if fold:
        left, right = left.casefold(), right.casefold()
    return left == right or left.startswith(right + os.sep)


# meiva:note
# The process that runs the check: its pid and process group, read once by the entry that
# starts it. The scan skips this process, its ancestors and its job tree.
@dataclass(frozen=True)
class Invoker:
    pid: int
    pgid: int | None


# meiva:note
# The invoker plus its ancestors — the pids the anchor scan skips.
#
# The check is normally invoked BY the cleanup that wants the removal, and
# that habit runs it from inside the target (`cd <worktree> && check`): the
# invoking shell's cwd then lies under the path and the scan would REFUSE a
# clean worktree (issue #3685) — tempting the caller into `--force`, which
# is exactly how a real anchor gets missed. The caller's chain is the
# remover, not an anchor; a long-lived session ON the chain is still
# protected by the pty-records arm. A transient gap in the chain (a parent
# reaped mid-walk) must not fail the scan.
def _caller_chain(pid: int) -> set[int]:
    chain = {pid}
    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        chain.update(parent.pid for parent in psutil.Process(pid).parents())
    return chain


def _pgid_of(pid: int) -> int | None:
    """`pid`'s process group, or None when it cannot be read (stale / denied).

    An unreadable group must read as "not the caller's" — never as a match.
    """
    if not hasattr(os, "getpgid"):  # pragma: no cover — non-POSIX
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _pty_anchors(run: Path, target: Path, *, fold: bool) -> list[str]:
    """Persistent pty sessions whose recorded cwd is under `target`.

    Asks the service on `run`'s socket (read-only: the home is never created); no
    service listening means no session.
    """
    return [
        f"pty session {info.name!r} (pid {info.pid}) cwd={info.cwd!r}"
        for info in client.list_sessions(socket_path=service_socket_path(run))
        if _under(info.cwd, target, fold=fold)
    ]


def _process_anchors(target: Path, *, fold: bool, invoker: Invoker) -> list[str]:
    """Live processes (outside the caller's job tree) with cwd / exe / argv under `target`."""
    hits: list[str] = []
    skip = _caller_chain(invoker.pid)
    group = invoker.pgid
    for proc in psutil.process_iter(["pid", "cwd", "exe", "cmdline"]):
        pid = proc.info["pid"]
        if pid in skip:
            continue
        if group is not None and _pgid_of(pid) == group:
            continue
        cwd = proc.info["cwd"]
        exe = proc.info["exe"]
        raw_cmdline = proc.info["cmdline"]
        cmdline = (
            [tok for tok in raw_cmdline if isinstance(tok, str)]
            if _is_obj_list(raw_cmdline)
            else []
        )
        if (
            _under(cwd, target, fold=fold)
            or _under(exe, target, fold=fold)
            or any(tok.startswith("/") and _under(tok, target, fold=fold) for tok in cmdline)
        ):
            hits.append(f"process {pid} cwd={cwd!r} exe={exe!r}")
    return hits


# meiva:note
# Human-readable list of live things anchored under `path`.
#
# `run` is the run directory holding the pty-sessions socket (`<home>/run`). The scan only
# reads: the home is never created, repaired or chmodded. Process scanning excludes the
# invoking job tree: the invoker's chain (issue #3685) and its process group,
# because the check is run by the cleanup from inside the target; a genuinely
# unrelated anchor (another session, a daemon) is in neither.
def find_live_anchors(path: Path, *, run: Path, invoker: Invoker) -> list[str]:
    target = path.resolve()
    fold = _folds_case(target)
    return _pty_anchors(run, target, fold=fold) + _process_anchors(
        target, fold=fold, invoker=invoker
    )
