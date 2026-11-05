# meiva:note
# What a round does to the world: judge one root, write the ledger, delete a tree, aggregate the
# ledger back into a report. The judgement itself lives in `decide.py`; the facts come from
# `evidence.py`; this module is the order the two are put in.

from __future__ import annotations

import datetime
import json
import os
from collections.abc import Mapping
from pathlib import Path

from base.host.boundary.untyped import object_of

from . import evidence
from .alerts import blind_alert
from .contracts import ProbeUnavailableError, Settings, TreeRow
from .decide import cooloff_block, on_never_list
from .evidence import probe_rows
from .values import int_of, object_dict, text_of


def record(settings: Settings, entry: Mapping[str, object]) -> None:
    # meiva:note
    # One line per decision that reached a tree, appended in the order the decisions happened —
    # the ledger is evidence, so it is never batched or reordered.
    line = dict(entry)
    line.setdefault("ts", datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
    with settings.ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def judge_root(
    settings: Settings, root: str | Path
) -> tuple[list[TreeRow], bool, dict[str, object]]:
    # meiva:note
    # `(rows, prefiltered)` — the trees of one root and whether a snapshot judge saw them.
    #
    # `prefiltered` is True only when 312's probe judged this root: the probe carries state
    # this tool does not own (a hold a human declared, the owner-approval ledger), so its
    # `reclaimable` verdict is the shortlist. Everywhere else — another checkout, or the
    # default root when the probe is blind — `atomic_ok`, the moment-of-deletion predicate,
    # is the whole judgement and every tree is a candidate for it.
    if os.path.realpath(root) == os.path.realpath(settings.repo):
        # meiva:note
        # A judge that is not installed is a known absence, not a failure: the run still has the
        # moment-of-deletion gate, so it reports and lets that gate decide every tree. Raising
        # here used to cost the whole root (`root-unreadable`) on a machine that simply has no
        # probe — a checkout testing this tool, or a probe file moved by a deploy.
        if not settings.probe or not settings.probe.is_file():
            print(
                f"{root}: snapshot judge not installed ({settings.probe or 'no --probe'}); "
                "the deletion gate decides every tree"
            )
            return evidence.git_trees(root), False, {}
        try:
            rows, line = probe_rows(settings)
        except ProbeUnavailableError as exc:
            # meiva:note
            # A ledger nobody is writing is a judge that stopped looking. Degrade to the
            # deletion gate and say which of the two reasons it was.
            print(
                f"{root}: snapshot judge unavailable ({exc}); the deletion gate decides every tree"
            )
            return evidence.git_trees(root), False, {}
        expected = len(
            [
                b
                for b in evidence.worktree_blocks(root)
                if os.path.realpath(b.get("worktree", "")) != os.path.realpath(root)
            ]
        )
        if len(rows) != expected:
            # meiva:note
            # 2026-11-03: the probe lost the `rows.append` for trees that exist — it landed
            # inside the `else` (path-gone) branch on 10-31 20:09 — so it reported
            # `trees: []` for two days while a dozen trees stood here, and this sweep
            # skipped every round as "no candidates". A judge that sees nothing looks
            # exactly like a repo with nothing to judge, so emptiness is never trusted:
            # compare against the tree list, say so once, and let the deletion gate — which
            # needs no snapshot — tell what it would take (deletion needs a flag).
            detail = f"probe listed {len(rows)} of {expected} trees"
            alert = blind_alert(settings, root, expected, detail)
            print(
                f"probe: 0 trees but {root} holds {expected} — snapshot judge is blind "
                f"({detail}); {alert}"
            )
            return evidence.git_trees(root), False, line
        return rows, True, line
    return evidence.git_trees(root), False, {}


def atomic_ok(
    settings: Settings, row: TreeRow, merge_cooloff_min: float, commit_cooloff_min: float
) -> tuple[bool, str, str]:
    # meiva:note
    # The moment-of-deletion predicate: re-read the facts that can change between the probe's
    # snapshot and `git worktree remove`, and refuse unless every condition the verdict rested on
    # still holds. 2026-10-20: two trees were to vanish under a waiter. If the probe ever grows a
    # new blocking condition, it belongs in `first_blocking_fact` too.
    blocked = first_blocking_fact(settings, row)
    if blocked:
        return False, blocked[0], blocked[1]
    merged_at = row.get("merged_at")
    blocked = cooloff_block(
        merged_at,
        evidence.minutes_since_iso(merged_at) if merged_at is not None else None,
        evidence.commit_age_minutes(row["path"]),
        merge_cooloff_min,
        commit_cooloff_min,
    )
    if blocked:
        return False, blocked[0], blocked[1]
    return True, "ok", "ok"


def first_blocking_fact(settings: Settings, row: TreeRow) -> tuple[str, str] | None:
    # meiva:note
    # The first tree-scoped fact that forbids deletion, in the order a reader would check it, or
    # None when the tree is eligible. Every code it returns is counted per run, which is what
    # makes "why did this tree stay" answerable from the ledger.
    #
    # Two steps on purpose: everything below the PR is answerable on this machine, and the PR is
    # the only question the network answers.
    local = local_block(row)
    if local:
        return local
    return pr_block(settings, row)


def local_block(row: TreeRow) -> tuple[str, str] | None:
    # meiva:note
    # The evidence this machine holds without asking anyone: the directory, the working tree, a
    # half-finished operation, the head, a live process, and the branch the tree is on.
    path = row["path"]
    if not Path(path).is_dir():
        return "gone", "gone"
    status, _, _ = evidence.sh(["git", "-C", path, "status", "--porcelain"], timeout=180)
    dirty = len([line for line in status.splitlines() if line.strip()])
    if dirty:
        return "dirty", f"dirty={dirty} now"
    # meiva:note
    # No stash veto here: `git stash` is repo-scoped, and one entry would freeze every tree under
    # that repo. `stash_report()` prints it on its own line (405, 2026-11-03).
    state = evidence.interrupted_state(path)
    if state:
        return "interrupted", f"{state} in progress"
    head = head_of(path)
    if not head.startswith(row.get("head") or "?"):
        return "head-moved", f"head moved ({head[:9] or '?'})"
    live = live_users(path)
    if live:
        return "live", f"live={live} now"
    branch = row.get("branch")
    if not branch:
        # meiva:note
        # Detached HEAD: there is no branch whose PR could prove the work in this tree is merged,
        # so the one thing this gate exists to prove is unknowable. `/private/tmp/verify-main`
        # looked "clean and old" and passed until this line existed.
        return "no-branch", "detached HEAD: no PR to prove the work is merged"
    if branch in ("main", "master"):
        # meiva:note
        # A tree holding the default branch has no "its own PR" to be merged: asking the API about
        # `main` would be asking about every merge at once. Never a candidate.
        return "on-default-branch", f"tree holds the default branch {branch}"
    return None


def head_of(path: str) -> str:
    # meiva:note
    # The tree's own HEAD, or an empty string when git cannot say — an unreadable head blocks the
    # tree at the comparison below rather than passing it.
    out, _, rc = evidence.sh(["git", "-C", path, "rev-parse", "HEAD"])
    return out if rc == 0 else ""


def live_users(path: str) -> int:
    # meiva:note
    # How many processes name this tree on their command line, excluding this tool's own: a
    # running process is a tree someone is using right now, whatever its age says.
    procs, _, _ = evidence.sh(["ps", "-axo", "command"], timeout=60)
    return len([line for line in procs.splitlines() if path in line and "worktree_gc" not in line])


def pr_block(settings: Settings, row: TreeRow) -> tuple[str, str] | None:
    # meiva:note
    # Merged *at this head* — the sha, not the branch name: a branch reused for the next task, or
    # moved by a post-merge push, must not hand this tree a MERGED. This is the one question that
    # costs a network round trip, so it runs last and only for a tree that passed everything else.
    out, _, rc = evidence.sh(
        [
            "gh",
            "pr",
            "view",
            row.get("branch") or "",
            "--json",
            "number,state,headRefOid,mergedAt",
            "-q",
            '(.number|tostring) + " " + .state + " " + .headRefOid + " " + (.mergedAt // "")',
        ],
        timeout=90,
        cwd=str(settings.repo),
    )
    fields = out.split()
    head = row.get("head") or "?"
    merged_at_head = len(fields) >= 3 and fields[1] == "MERGED" and fields[2].startswith(head[:9])
    if rc != 0 or not merged_at_head:
        return "pr-not-merged-at-head", f"pr no longer merged at this tip ({out[:60]})"
    row["resolved_pr"] = int(fields[0])
    row["merged_at"] = fields[3] if len(fields) > 3 else ""
    return None


def reclaim(
    settings: Settings,
    root: str,
    row: TreeRow,
    *,
    dry_run: bool,
    merge_cooloff_min: float,
    commit_cooloff_min: float,
) -> dict[str, object]:
    # meiva:note
    # Reclaim one tree, or explain why not. Returns the ledger entry; the caller records it, so a
    # decision is written even when it is "skipped".
    path = row["path"]
    entry = ledger_entry(
        root,
        row,
        dry_run=dry_run,
        merge_cooloff_min=merge_cooloff_min,
        commit_cooloff_min=commit_cooloff_min,
    )
    if on_never_list(Path(path).name, evidence.never_patterns(settings)):
        return {**entry, "result": "skipped-never-list"}
    holder = evidence.held(settings, Path(path).name)
    if holder:
        return {
            **entry,
            "result": "skipped-hold",
            "blocked_by": "hold",
            "verify": f"held by {holder}",
        }
    ok, code, line = atomic_ok(settings, row, merge_cooloff_min, commit_cooloff_min)
    entry["verify"] = line
    resolved = row.get("resolved_pr")
    if resolved and not entry["pr"]:
        # meiva:note
        # The gate resolved the PR for this tip; keep the number, not just the verdict.
        entry["pr"] = [resolved]
    entry["blocked_by"] = None if ok else code
    if not ok:
        # meiva:note
        # The snapshot said yes and the moment says no — the moment wins.
        return {**entry, "result": "skipped-verify"}
    if dry_run:
        return {**entry, "result": "would-reclaim"}
    return remove_tree(root, path, row, entry)


def ledger_entry(
    root: str,
    row: TreeRow,
    *,
    dry_run: bool,
    merge_cooloff_min: float,
    commit_cooloff_min: float,
) -> dict[str, object]:
    # meiva:note
    # The facts a decision was made on, written before the decision is taken: a ledger that only
    # records outcomes cannot answer "what did the judge see that night".
    return {
        "tree": Path(row["path"]).name,
        "root": root,
        "path": row["path"],
        "branch": row.get("branch"),
        "tip": row.get("head"),
        "pr": [p.get("number") for p in (row.get("pr") or []) if p.get("state") == "MERGED"],
        "verdict": row.get("verdict"),
        "size_gib": row.get("size_gib"),
        "dry_run": dry_run,
        "merge_cooloff_min": merge_cooloff_min,
        "commit_cooloff_min": commit_cooloff_min,
    }


def remove_tree(root: str, path: str, row: TreeRow, entry: dict[str, object]) -> dict[str, object]:
    # meiva:note
    # `worktree remove` first; `prune` only picks up after it, never as a substitute. Branch refs
    # are kept in every root (405, 2026-11-03): `worktree remove` deletes no ref, a ref costs a few
    # hundred bytes, and the tree can be rebuilt from it in seconds — deleting it was the only
    # irreversible half of this operation.
    before = evidence.tree_size_gib(path) or row.get("size_gib") or 0
    out, err, rc = evidence.sh(
        ["git", "-C", root, "worktree", "remove", "--force", path], timeout=600
    )
    if rc != 0:
        return {**entry, "result": "remove-failed", "error": (err or out)[-300:]}
    evidence.sh(["git", "-C", root, "worktree", "prune"], timeout=120)
    entry.update({"result": "reclaimed", "freed_gib": before, "branch_kept": row.get("branch")})
    return entry


def ledger_report(settings: Settings, hours: float) -> int:
    # meiva:note
    # Aggregate the per-run tallies over the last `hours` — the line the daily report quotes.
    #
    # Without it, "the cool-off works" is a claim nobody can check: every round records what
    # each candidate was blocked by, so this is one groupby over the ledger.
    tally: dict[str, int] = {}
    rounds, candidates = 0, 0
    with settings.ledger.open(encoding="utf-8") as handle:
        for line in handle:
            entry = object_dict(object_of(json.loads, line))
            stamp = evidence.minutes_since_iso(text_of(entry.get("ts")))
            if stamp is None or stamp > hours * 60:
                continue
            if entry.get("result") in ("run-summary", "skip-no-candidates"):
                rounds += 1
                candidates += int_of(entry.get("candidates"))
                for key, count in object_dict(entry.get("tally")).items():
                    tally[key] = tally.get(key, 0) + int_of(count)
    outcome = ", ".join(f"{key}={count}" for key, count in sorted(tally.items())) or "nothing"
    print(f"last {hours:g}h: {rounds} open rounds, {candidates} candidates; outcome: {outcome}")
    return 0
