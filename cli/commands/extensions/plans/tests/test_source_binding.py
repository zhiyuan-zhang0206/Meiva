# meiva:note
# When a worktree checkout may sync skills into a home, decided from values
# (`plans/source_binding.py`). The shells that resolve the paths are exercised in
# `test_skills_sync.py` and `test_skill_update.py`.

from __future__ import annotations

import pytest

from cli.commands.extensions.plans.source_binding import SourceBinding, worktree_sync_refusal

_PROD = "/home/u/.meiva"
_WORKTREE = "/home/u/Meiva/.worktrees/meiva-9999-task"
_CHECKOUT = "/home/u/Meiva"


@pytest.mark.parametrize(
    "worktree", ["/home/u/Meiva/.worktrees/x", "/home/u/p/.claude/worktrees/y"]
)
def test_a_worktree_checkout_is_refused_for_the_prod_home(worktree: str) -> None:
    refusal = worktree_sync_refusal(
        SourceBinding(default_home=_PROD, allow_worktree=False), repo=worktree, home=_PROD
    )

    assert refusal is not None
    assert worktree in refusal and _PROD in refusal and "MEIVA_CONVERGE_ALLOW_WORKTREE=1" in refusal


@pytest.mark.parametrize(
    ("repo", "home", "allow_worktree"),
    [
        (_WORKTREE, "/home/u/.meiva-dev", False),
        (_CHECKOUT, _PROD, False),
        (_WORKTREE, _PROD, True),
    ],
)
def test_every_other_combination_may_sync(repo: str, home: str, allow_worktree: bool) -> None:
    binding = SourceBinding(default_home=_PROD, allow_worktree=allow_worktree)

    assert worktree_sync_refusal(binding, repo=repo, home=home) is None
