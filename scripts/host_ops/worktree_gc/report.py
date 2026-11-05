# meiva:note
# The round's output, one line per fact a reader has to be able to grep: the disk banner, one
# line per root, one line per candidate, and a machine-readable tally. The format is a contract
# with the schedule's alerts — the per-tree line carried a verdict column once and a regex on it
# silently stopped matching, which is why the tally line exists.

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from .contracts import StashReport, SurfaceReport, TreeRow
from .values import float_of, text_of


def print_stash(stashes: Sequence[StashReport]) -> None:
    # meiva:note
    # One line per repo — the report half of the scope rule.
    for item in stashes:
        if item.get("count") is None:
            print("stash: {} unreadable ({})".format(item.get("root"), item.get("error")))
            continue
        if not item.get("count"):
            print("stash: {} 0 entries".format(item.get("root")))
            continue
        entries = item.get("entries") or []
        oldest = entries[0]
        for entry in entries:
            when = text_of(entry.get("when"))
            current = text_of(oldest.get("when"))
            if when and current and when < current:
                oldest = entry
        when = (text_of(oldest.get("when")) or "?")[:10]
        debt = "DEBT(>30d)" if item.get("debt") else ""
        print(
            f"stash: {item.get('root')} {item.get('count')} entries, oldest {when} "
            f"({float_of(item.get('oldest_days')):.1f} days) {debt} — repo-scoped, reported only"
        )


def print_surface(surface: SurfaceReport) -> None:
    # meiva:note
    # One line per root, and one line per ghost — the report half of the surface check.
    if surface["missing_dirs"]:
        names = ", ".join(Path(path).name for path in surface["missing_dirs"])
        print(
            f"surface: {len(surface['missing_dirs'])} registered tree(s) have no directory "
            f"(prune clears the registration): {names}"
        )
    if surface["ghosts"]:
        for ghost in surface["ghosts"]:
            print(
                f"surface: GHOST {text_of(ghost.get('tree'))} "
                f"({float_of(ghost.get('size_gib')):.2f} GiB) — .git "
                "points into this repo but nothing registers it; neither prune nor the gate "
                "will ever free it — report only"
            )
    if surface["foreign"]:
        foreign = ", ".join(
            f"{text_of(item.get('tree'))} -> {text_of(item.get('gitdir')).split('/.git/')[0]}"
            for item in surface["foreign"]
        )
        print(
            f"surface: {len(surface['foreign'])} director(y/ies) under .worktrees belong to "
            f"another repository (never touched): {foreign}"
        )
    if surface["not_a_tree"]:
        others = ", ".join(text_of(item.get("tree")) for item in surface["not_a_tree"])
        print(
            f"surface: {len(surface['not_a_tree'])} entr(y/ies) under .worktrees are not "
            f"worktrees at all (no .git; never touched): {others}"
        )


def banner(
    avail: float,
    pct: float,
    trigger_avail_gib: float,
    trigger_used_pct: float,
    *,
    gated: bool,
) -> str:
    # meiva:note
    # One line that says which way the gate went and on what reading, so a quiet night and a
    # gated night are told apart without reading the ledger.
    state = "OPEN" if gated else "closed"
    return (
        f"disk: avail={avail:.1f}GiB used={pct:.1f}% "
        f"gate(avail<{trigger_avail_gib:.0f}G or used>={trigger_used_pct:.0f}%) -> {state}"
    )


def trees_line(root: str | Path, rows: Sequence[TreeRow]) -> str:
    # meiva:note
    # What the judge saw, by name and verdict: the line that makes a blind probe visible.
    seen = {Path(row["path"]).name: row.get("verdict") for row in rows}
    return f"{root}: {len(rows)} trees, verdicts={json.dumps(seen, ensure_ascii=False)}"


def tree_line(entry: Mapping[str, object]) -> str:
    # meiva:note
    # One candidate: its size, why it was skipped or taken, the moment-of-deletion detail, and
    # what it freed. The verdict column sits between the size and the number on purpose — a
    # parser that splits on whitespace reads the tally line instead.
    key = text_of(entry.get("blocked_by")) or text_of(entry.get("result"))
    freed = f" ({float_of(entry.get('freed_gib')):.2f} GiB)" if entry.get("freed_gib") else ""
    size = f"{float_of(entry.get('size_gib')):5.2f}G " if entry.get("size_gib") else "   ?   "
    return f"{text_of(entry.get('tree')):<32} {size}{key:<22} {text_of(entry.get('verify'))}{freed}"


def tally_line(tally: Mapping[str, int]) -> str:
    # meiva:note
    # Machine-readable on purpose: `tally: merge-cooloff=3,commit-cooloff=1` is what the
    # schedule's alert greps, so every key is counted and the line is never empty.
    counted = ", ".join(f"{key}={count}" for key, count in sorted(tally.items()))
    return "tally: " + (counted or "nothing")
