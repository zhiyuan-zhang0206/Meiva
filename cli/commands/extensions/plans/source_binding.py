# meiva:note
# When a skills sync from a checkout may write a home's load directory.
#
# A git worktree checkout's sources are branch work-in-progress. Converge and `meiva skill
# update` take their sources from the checkout the CLI runs from, and in a worktree-heavy
# dev environment that assumption is false exactly when the CLI is run from a
# `.worktrees/...` or `.claude/worktrees/...` checkout while the resolved home is the default
# prod home: the worktree's branch content, possibly unmerged, would land in `~/.meiva/skills`
# and silently shadow what main ships (observed 2026-08-08, audit round 2, skills-plugins #3).
# Only the prod-home and worktree combination is refused; a home of any other name (a test or a
# scratch home) takes whatever checkout runs.
#
# Values in, the refusal out. Paths arrive resolved, because resolving reads the filesystem.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SourceBinding", "worktree_sync_refusal"]

_WORKTREE_MARKERS = (".claude/worktrees", "/.worktrees/")


# meiva:note
# What the sync's invoker says about the prod home: `default_home` is the resolved path of the
# user's default home (`~/.meiva`), and `allow_worktree` is the operator's explicit override
# (`MEIVA_CONVERGE_ALLOW_WORKTREE=1`).
@dataclass(frozen=True)
class SourceBinding:
    default_home: str
    allow_worktree: bool


# meiva:note
# The reason `repo` (resolved) may not be synced into `home` (resolved), or None when it may.
def worktree_sync_refusal(binding: SourceBinding, *, repo: str, home: str) -> str | None:
    if binding.allow_worktree:
        return None
    if home == binding.default_home and any(marker in repo for marker in _WORKTREE_MARKERS):
        return (
            f"refusing to sync skills from worktree checkout {repo} into the prod "
            f"home {home}: a worktree's sources are branch work-in-progress and "
            f"must not replace prod's load dir (run `meiva` from the prod checkout, "
            f"or set MEIVA_CONVERGE_ALLOW_WORKTREE=1 to override)"
        )
    return None
