# meiva:note
# The prod home a skills-sync test stands beside.
#
# `PROD_ELSEWHERE` says the user's default home is some other directory, so a test's own home is a
# scratch home and any checkout may sync into it. `prod_home_is(home)` makes a test's own home the
# prod home, which is how the worktree refusal is seen.

from __future__ import annotations

from pathlib import Path

from cli.commands.extensions.plans.source_binding import SourceBinding

__all__ = ["PROD_ELSEWHERE", "prod_home_is"]

PROD_ELSEWHERE = SourceBinding(default_home="/nonexistent/prod-home/.meiva", allow_worktree=False)


def prod_home_is(home: Path, *, allow_worktree: bool = False) -> SourceBinding:
    return SourceBinding(default_home=str(home.resolve()), allow_worktree=allow_worktree)
