# meiva:note
# The round's output, one line per fact a reader has to be able to grep: the disk banner, one
# line per root, one line per candidate, and a machine-readable tally. The format is a contract
# with the schedule's alerts — the per-tree line carried a verdict column once and a regex on it
# silently stopped matching, which is why the tally line exists.

from __future__ import annotations

import json
from pathlib import Path


def print_stash(stashes):
    # meiva:note
    # One line per repo — the report half of the scope rule.
    for item in stashes:
        if item.get("count") is None:
            print("stash: {} unreadable ({})".format(item["root"], item.get("error")))
            continue
        if not item["count"]:
            print("stash: {} 0 entries".format(item["root"]))
            continue
        oldest = item["entries"][0]
        for entry in item["entries"]:
            if entry.get("when") and (entry["when"] < oldest.get("when", entry["when"])):
                oldest = entry
        when = (oldest.get("when") or "?")[:10]
        debt = "DEBT(>30d)" if item["debt"] else ""
        print(
            f"stash: {item['root']} {item['count']} entries, oldest {when} "
            f"({item['oldest_days'] or 0.0:.1f} days) {debt} — repo-scoped, reported only"
        )


def print_surface(surface):
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
                f"surface: GHOST {ghost['tree']} ({ghost['size_gib'] or 0.0:.2f} GiB) — .git "
                "points into this repo but nothing registers it; neither prune nor the gate "
                "will ever free it — report only"
            )
    if surface["foreign"]:
        foreign = ", ".join(
            f"{item['tree']} -> {item['gitdir'].split('/.git/')[0]}" for item in surface["foreign"]
        )
        print(
            f"surface: {len(surface['foreign'])} director(y/ies) under .worktrees belong to "
            f"another repository (never touched): {foreign}"
        )
    if surface["not_a_tree"]:
        others = ", ".join(item["tree"] for item in surface["not_a_tree"])
        print(
            f"surface: {len(surface['not_a_tree'])} entr(y/ies) under .worktrees are not "
            f"worktrees at all (no .git; never touched): {others}"
        )


def banner(avail, pct, trigger_avail_gib, trigger_used_pct, gated):
    # meiva:note
    # One line that says which way the gate went and on what reading, so a quiet night and a
    # gated night are told apart without reading the ledger.
    state = "OPEN" if gated else "closed"
    return (
        f"disk: avail={avail:.1f}GiB used={pct:.1f}% "
        f"gate(avail<{trigger_avail_gib:.0f}G or used>={trigger_used_pct:.0f}%) -> {state}"
    )


def trees_line(root, rows):
    # meiva:note
    # What the judge saw, by name and verdict: the line that makes a blind probe visible.
    seen = {Path(row["path"]).name: row.get("verdict") for row in rows}
    return f"{root}: {len(rows)} trees, verdicts={json.dumps(seen, ensure_ascii=False)}"


def tree_line(entry):
    # meiva:note
    # One candidate: its size, why it was skipped or taken, the moment-of-deletion detail, and
    # what it freed. The verdict column sits between the size and the number on purpose — a
    # parser that splits on whitespace reads the tally line instead.
    key = entry.get("blocked_by") or entry["result"]
    freed = f" ({entry['freed_gib']:.2f} GiB)" if entry.get("freed_gib") else ""
    size = f"{entry['size_gib']:5.2f}G " if entry.get("size_gib") else "   ?   "
    return f"{entry['tree']:<32} {size}{key:<22} {entry.get('verify') or ''}{freed}"


def tally_line(tally):
    # meiva:note
    # Machine-readable on purpose: `tally: merge-cooloff=3,commit-cooloff=1` is what the
    # schedule's alert greps, so every key is counted and the line is never empty.
    counted = ", ".join(f"{key}={count}" for key, count in sorted(tally.items()))
    return "tally: " + (counted or "nothing")
