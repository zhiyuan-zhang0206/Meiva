# meiva:note
# The judgements, as functions of facts: no file, no clock, no subprocess. Every branch here is
# a rule that can be tested by handing it a value, which is the point of the split — before it,
# these rules only existed inside a function that also read git and `gh`.
# `scripts/host_ops/docs/worktree-gc.meiva.okf.md` owns what each rule is for.

from __future__ import annotations

import fnmatch
from collections.abc import Sequence


def cooloff_block(
    merged_at: str | None,
    merged_age_min: float | None,
    commit_age_min: float | None,
    merge_cooloff_min: float,
    commit_cooloff_min: float,
) -> tuple[str, str] | None:
    # meiva:note
    # (code, detail) while a clock still protects this tree, or None to let it go.
    #
    # `merged_at` None means there is no PR clock to read (a tree without a branch); an empty
    # string means `gh` answered without a `mergedAt`, which is unreadable time, not absent time.
    # Both codes are counted per run, so the daily report can say *why* trees stayed.
    if merged_at is not None:
        if merged_age_min is None:
            return "merge-time-unknown", f"merge time unreadable ({merged_at!r})"
        if merged_age_min < merge_cooloff_min:
            return "merge-cooloff", f"merged {merged_age_min:.0f} min ago (< {merge_cooloff_min:g})"
    if commit_age_min is None:
        return "commit-age-unknown", "last commit age unreadable"
    if commit_age_min < commit_cooloff_min:
        return (
            "commit-cooloff",
            f"committed {commit_age_min:.0f} min ago (< {commit_cooloff_min:g})",
        )
    return None


def on_never_list(name: str, patterns: Sequence[str]) -> bool:
    # meiva:note
    # A never-list entry is a human's declaration that a tree is off-limits. Matching is
    # case-insensitive on the directory name only — never on a path — so one entry cannot
    # silence a whole root.
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in patterns)
