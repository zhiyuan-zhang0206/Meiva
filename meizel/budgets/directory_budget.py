# meiva:note
# The effect shell of the file budget's path reading: it lists one directory of the checkout, walks
# a judged path's links and resolves the target names against the repository. Which entries and
# which paths the ceilings judge is decided in `meizel.budgets.budget_plans`.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

import os
from pathlib import Path

from meizel.budgets import budget_plans

__all__ = ["entries", "selected_under"]


# meiva:note
# The candidates of the file budget's Python scope at one directory level, without following a
# link: `scandir` reports each entry's kind from the directory read itself, so classifying one costs
# no further call on the filesystem.
def entries(directory: Path) -> list[Path]:
    with os.scandir(directory) as scan:
        return [
            directory / entry.name
            for entry in scan
            if budget_plans.entry_candidate(
                entry.name,
                is_symlink=entry.is_symlink(),
                is_directory=entry.is_dir(follow_symlinks=False),
            )
        ]


# meiva:note
# The judged path for one target and one structure directory, without following a link: the side
# `judged_side` picks, refused when the path or any of its ancestors is a link.
def selected_under(target: Path, scope: Path, repo_root: Path) -> Path | None:
    side = budget_plans.judged_side(target, scope, repo_root)
    if side is None:
        return None
    selected = target if side == "target" else scope
    if any(path.is_symlink() for path in (selected, *selected.parents)):
        return None
    return selected
