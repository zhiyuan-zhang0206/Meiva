# meiva:note
# Everything that reads the world: git, `du`, `ps`, another agent's ledger, the hold file, the
# never-list. Nothing here decides anything — a function returns what it saw and `decide.py`
# judges it. `scripts/host_ops/docs/worktree-gc.meiva.okf.md` owns why each fact is read the
# way it is.

from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of

from .contracts import (
    PROBE_MAX_AGE_S,
    ProbeUnavailableError,
    Settings,
    StashReport,
    SurfaceEntry,
    SurfaceReport,
    TreeRow,
)
from .values import object_dict, object_rows

# meiva:note
# The three fields the probe writes on a tree row beside its verdict; every other key it may grow
# belongs to it, not to this tool, and is not carried into the ledger.
NUMERIC_FIELDS = ("size_gib",)


def sh(args: Sequence[str], timeout: float = 90, cwd: str | None = None) -> tuple[str, str, int]:
    # meiva:note
    # The only way this tool runs a command: a fixed argv built here, never a shell string.
    proc = subprocess.run(  # noqa: S603
        args, capture_output=True, text=True, timeout=timeout, cwd=cwd, check=False
    )
    return proc.stdout.strip(), proc.stderr.strip(), proc.returncode


def number_of(value: object) -> float | None:
    # meiva:note
    # A reading that may be absent — a size, an age — as a float, or None when the producer did not
    # supply one. None is a fact here ("unreadable"), so zero is never its stand-in.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def tree_rows(value: object) -> list[TreeRow]:
    # meiva:note
    # The probe's tree list read as rows this tool can judge: the fields below are the ones it
    # reads back, and a row without even a path is dropped rather than judged as an empty name.
    rows: list[TreeRow] = []
    for item in object_rows(value):
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        row: TreeRow = {"path": path}
        branch = item.get("branch")
        if isinstance(branch, str):
            row["branch"] = branch
        head = item.get("head")
        if isinstance(head, str):
            row["head"] = head
        size = number_of(item.get("size_gib"))
        if size is not None:
            row["size_gib"] = size
        verdict = item.get("verdict")
        if isinstance(verdict, str):
            row["verdict"] = verdict
        pr = item.get("pr")
        if is_object_list(pr):
            row["pr"] = [dict(entry) for entry in pr if is_object_dict(entry)]
        rows.append(row)
    return rows


def disk_state(settings: Settings) -> tuple[float, float]:
    # meiva:note
    # (available GiB, used percent) for the volume holding the repo.
    out, err, rc = sh(["df", "-k", str(settings.repo)])
    if rc != 0 or len(out.splitlines()) < 2:
        raise RuntimeError("df failed: %s" % (err or out))
    fields = out.splitlines()[-1].split()
    return int(fields[3]) / 1048576.0, float(fields[4].rstrip("%"))


def stash_report(root: str | Path, trees: Sequence[TreeRow]) -> StashReport:
    # meiva:note
    # Repo-scoped stash inventory — reported, never a veto (405, 2026-11-03).
    #
    # A stash entry lives in the shared repo, so one of them used to freeze *every* tree under
    # it: `git stash list` run from any of them prints the same list. What is actually at stake
    # is answerable on its own line: how many, how old the oldest one is, and whether the commit
    # each was taken on still has a tree that contains it. Older than 30 days is reported as
    # debt, not as a reason to keep a tree.
    out, err, rc = sh(
        ["git", "-C", str(root), "stash", "list", "--format=%gd\t%ci\t%h\t%s"], timeout=60
    )
    if rc != 0:
        return {"root": str(root), "count": None, "error": (err or out)[-200:]}
    heads = [head for row in trees if (head := row.get("head"))]
    counted = [stash_row(root, line, heads) for line in out.splitlines() if line.strip()]
    ages = [age for _, age in counted if age is not None]
    oldest_days = max(ages) if ages else None
    return {
        "root": str(root),
        "count": len(counted),
        "oldest_days": oldest_days,
        "debt": bool(oldest_days is not None and oldest_days > 30),
        "entries": [row for row, _ in counted],
    }


def stash_row(
    root: str | Path, line: str, heads: Sequence[str]
) -> tuple[dict[str, object], float | None]:
    # meiva:note
    # One stash entry, and its age in days. `in_a_tree` answers the only question that matters
    # when deciding whether the stashed work still exists somewhere: is its base an ancestor of
    # any tree this root registers? None means unreadable, which is not the same as "no".
    ref, when, tip, subject = ([*line.split("\t"), "", "", ""])[:4]
    base, _, rc_base = sh(["git", "-C", str(root), "rev-parse", ref + "^1"], timeout=60)
    base = base.strip() if rc_base == 0 else ""
    anchored = None
    if base:
        anchored = any(
            sh(["git", "-C", str(root), "merge-base", "--is-ancestor", base, head], timeout=60)[2]
            == 0
            for head in heads
        )
    row: dict[str, object] = {
        "ref": ref,
        "when": when,
        "tip": tip,
        "subject": subject[:80],
        "base": base[:12],
        "in_a_tree": anchored,
    }
    age = minutes_since_iso(when)
    return row, (round(age / 1440.0, 1) if age is not None else None)


def surface_report(root: str | Path) -> SurfaceReport:
    # meiva:note
    # Registered trees vs directories under `<root>/.worktrees` — both directions, scoped.
    #
    # `git worktree list` is the only enumeration this tool may act on: a directory is not
    # evidence that a tree is registered. But the two surfaces can disagree, and before
    # 2026-11-03 nothing looked:
    #
    #   * a registered tree whose directory is gone (a zombie registration — `prune` clears it)
    #   * a directory whose `.git` points *into this repo* but is not registered (a ghost that
    #     holds disk and that no `prune` will ever free)
    #
    # The scope is the whole point. `~/.meiva/worktrees/` on company-mini holds four healthy
    # worktrees of the Monsora checkout, registered in *that* repo; a parent-directory diff
    # would have called them ghosts and a "cleanup" would have deleted live work. So a
    # directory counts only when its `.git` file resolves inside this root's git dir; anything
    # else is reported as foreign and never touched. Read-only, and it deletes nothing.
    registered = {os.path.realpath(b.get("worktree", "")): b for b in worktree_blocks(root)}
    report: SurfaceReport = {
        "root": str(root),
        "registered": len(registered),
        "missing_dirs": [],
        "ghosts": [],
        "foreign": [],
        "not_a_tree": [],
    }
    for path, block in registered.items():
        if block.get("worktree") and not Path(path).is_dir():
            report["missing_dirs"].append(path)
    parent = Path(root) / ".worktrees"
    try:
        names = sorted(entry.name for entry in parent.iterdir())
    except OSError:
        return report
    for name in names:
        path = str(parent / name)
        if not Path(path).is_dir() or os.path.realpath(path) in registered:
            continue
        entry: SurfaceEntry = {"tree": name, "path": path, "size_gib": tree_size_gib(path)}
        try:
            with (Path(path) / ".git").open(encoding="utf-8") as handle:
                pointer = handle.readline().strip()
        except OSError:
            pointer = ""
        gitdir = pointer.split("gitdir:", 1)[1].strip() if pointer.startswith("gitdir:") else ""
        if not gitdir:
            report["not_a_tree"].append(entry)
        elif os.path.realpath(gitdir).startswith(os.path.realpath(root) + os.sep):
            report["ghosts"].append(entry)
        else:
            entry["gitdir"] = gitdir
            report["foreign"].append(entry)
    return report


def held(settings: Settings, name: str) -> str | None:
    # meiva:note
    # The hold 312 keeps for trees a human declared off-limits, read read-only.
    #
    # A hold is not this tool's state, so it is only consulted — never rewritten; the
    # expired-notice bookkeeping stays where it belongs. Only the probe path can honour it
    # through the probe's own verdict, so the gate consults it directly as well.
    try:
        with settings.hold.open(encoding="utf-8") as handle:
            holds = object_dict(object_of(json.load, handle))
    except (OSError, ValueError):
        return None
    entry = holds.get(name)
    if not is_object_dict(entry):
        return None
    expires = entry.get("expires_at")
    if isinstance(expires, str) and expires:
        with suppress(ValueError):
            if datetime.datetime.fromisoformat(expires).timestamp() <= time.time():
                return None
    by = entry.get("by", "owner")
    if isinstance(by, str):
        return by or None
    return None if by is None else str(by)


def never_patterns(settings: Settings) -> list[str]:
    try:
        with settings.never.open(encoding="utf-8") as handle:
            lines = [line.strip() for line in handle]
    except OSError:
        return []
    return [line for line in lines if line and not line.startswith("#")]


def probe_rows(settings: Settings) -> tuple[list[TreeRow], dict[str, object]]:
    # meiva:note
    # Read 312's probe from its ledger; return (tree rows, the ledger line that carried them).
    #
    # The line — not the rows — carries `probe_sha`/`probe_path`/`repo` (312, 2026-11-03), so the
    # caller can record *which* judge ran this round instead of only that one did.
    #
    # This tool does not start the probe. The probe is another agent's program living outside this
    # repository, and the repository's restricted-Python rule forbids an in-repo file from spawning
    # an interpreter on a path it cannot see; running it is the composition root's job (the schedule
    # runs `probe` and then this tool). What this side owns is the freshness question: a ledger that
    # stopped being written is a judge that stopped looking, and a judge that stopped looking must
    # read as "no snapshot judge" rather than as "nothing to reclaim".
    try:
        with settings.probe_ledger.open(encoding="utf-8") as handle:
            last = handle.readlines()[-1]
    except (OSError, IndexError) as exc:
        raise ProbeUnavailableError(f"no probe ledger at {settings.probe_ledger} ({exc})") from exc
    try:
        payload = object_dict(object_of(json.loads, last))
    except ValueError as exc:
        raise ProbeUnavailableError(f"probe ledger line is not JSON: {exc}") from exc
    age = _ledger_age_s(settings, payload)
    if age is None:
        raise ProbeUnavailableError("probe ledger line carries no timestamp")
    if age > PROBE_MAX_AGE_S:
        raise ProbeUnavailableError(
            f"probe ledger is {age:.0f} s old (limit {PROBE_MAX_AGE_S:.0f} s)"
        )
    return tree_rows(payload.get("trees")), payload


def _ledger_age_s(settings: Settings, payload: dict[str, object]) -> float | None:
    # meiva:note
    # How old the probe's line is, from its own stamp or the ledger file's mtime.
    for key in ("ts", "at", "generated_at", "time"):
        stamp = payload.get(key)
        if isinstance(stamp, str):
            try:
                parsed = datetime.datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            return (datetime.datetime.now().astimezone() - parsed).total_seconds()
    try:
        return time.time() - settings.probe_ledger.stat().st_mtime
    except OSError:
        return None


def worktree_blocks(root: str | Path) -> list[dict[str, str]]:
    # meiva:note
    # `git worktree list --porcelain` for one root, split into per-tree dicts.
    out, err, rc = sh(["git", "-C", str(root), "worktree", "list", "--porcelain"], timeout=120)
    if rc != 0:
        raise RuntimeError(f"worktree list failed for {root}: {(err or out)[-300:]}")
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in [*out.splitlines(), ""]:
        if not line:
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        key, _, value = line.partition(" ")
        cur[key] = value
    return blocks


def git_trees(root: str | Path) -> list[TreeRow]:
    # meiva:note
    # The tree list of one root, as rows the deletion gate can judge on its own.
    #
    # Used for every root except the default one. There is no snapshot verdict here and
    # that is deliberate: `atomic_ok` — the moment-of-deletion predicate — is already a
    # superset of the snapshot criteria (clean, HEAD unmoved, no live anchor, PR merged at
    # this tip, both cool-offs), so a second copy of the judgement would only be a second
    # thing to drift.
    trees: list[TreeRow] = []
    for block in worktree_blocks(root):
        path = block.get("worktree", "")
        if not path or os.path.realpath(path) == os.path.realpath(root):
            continue
        trees.append(
            {
                "path": path,
                "branch": (block.get("branch") or "").replace("refs/heads/", "") or None,
                "head": (block.get("HEAD") or "")[:9],
                "size_gib": tree_size_gib(path),
                "verdict": "unjudged",
            }
        )
    return trees


def tree_size_gib(path: str | Path) -> float | None:
    out, _, rc = sh(["du", "-sk", str(path)], timeout=600)
    if rc != 0 or not out:
        return None
    return round(int(out.split()[0]) / 1048576.0, 2)


def minutes_since_iso(stamp: str | None) -> float | None:
    # meiva:note
    # Minutes from an ISO-8601 instant (`gh` prints `...Z`) to now; None if unreadable.
    if not stamp:
        return None
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    return (datetime.datetime.now(datetime.UTC) - moment).total_seconds() / 60.0


def commit_age_minutes(path: str | Path) -> float | None:
    # meiva:note
    # Minutes since the tree's last commit; None when it cannot be read.
    out, _, rc = sh(["git", "-C", str(path), "log", "-1", "--format=%ct"], timeout=60)
    stamp = out.strip()
    if rc != 0 or not stamp.isdigit():
        return None
    now = datetime.datetime.now(datetime.UTC).timestamp()
    return (now - int(stamp)) / 60.0


def interrupted_state(path: str | Path) -> str | None:
    # meiva:note
    # Name of a half-finished rebase/merge/cherry-pick in this tree, or None.
    #
    # A tree-scoped fact, so it may veto: git keeps these states in the worktree's own
    # admin dir and `status --porcelain` can be empty in the middle of one. Deleting a
    # tree mid-rebase throws away the conflict resolution the owner is in the middle of.
    out, _, rc = sh(["git", "-C", str(path), "rev-parse", "--git-dir"], timeout=60)
    if rc != 0 or not out:
        return None
    gitdir = Path(out) if Path(out).is_absolute() else Path(path) / out
    for marker, label in (
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
    ):
        if (Path(gitdir) / marker).exists():
            return label
    return None
