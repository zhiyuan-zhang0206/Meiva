# meiva:note
# The composition root: the only module that parses arguments, resolves the paths once, and
# decides the order of a round. Nothing here judges a tree — `judge_root` in `act.py` does that —
# and nothing here reads git; the walk below is the order the work happens in.

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
from pathlib import Path

from . import act, evidence, report
from .contracts import (
    AVAIL_TRIGGER_GIB,
    COMMIT_COOLOFF_MIN,
    MAX_PER_RUN,
    MERGE_COOLOFF_MIN,
    USED_PCT_TRIGGER,
    Settings,
)


@dataclasses.dataclass
class Round:
    # meiva:note
    # One run's readings and tallies, so no function has to carry nine counters in its signature.
    avail: float = 0.0
    pct: float = 0.0
    gated: bool = False
    tally: dict = dataclasses.field(default_factory=dict)
    judged: dict = dataclasses.field(default_factory=dict)
    surfaces: list = dataclasses.field(default_factory=list)
    stashes: list = dataclasses.field(default_factory=list)
    roots: list = dataclasses.field(default_factory=list)
    seen: int = 0
    candidates: int = 0
    freed: float = 0.0


def main(argv=None):
    args = build_parser().parse_args(argv)
    settings = settings_from(args)
    if args.report_hours:
        return act.ledger_report(settings, args.report_hours)
    round_ = open_round(settings, args)
    for root in settings.roots:
        sweep_root(settings, root, args, round_)
    return finish(settings, args, round_)


def open_round(settings: Settings, args):
    round_ = Round(roots=[str(root) for root in settings.roots])
    round_.avail, round_.pct = evidence.disk_state(settings)
    round_.gated = round_.avail < args.trigger_avail_gib or round_.pct >= args.trigger_used_pct
    print(
        report.banner(
            round_.avail, round_.pct, args.trigger_avail_gib, args.trigger_used_pct, round_.gated
        )
    )
    return round_


def sweep_root(settings: Settings, root, args, round_):
    # meiva:note
    # The judge runs whatever the gate says. A blind probe is exactly the failure that must not
    # wait for disk pressure to be discovered — the gate decides whether to reclaim, never
    # whether to look (405, 2026-11-03). Only reading is unconditional; the moment-of-deletion
    # gate below still costs a `gh` per tree and stays behind the disk gate.
    try:
        rows, prefiltered, probe_line = act.judge_root(settings, root)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # meiva:note
        # One unreadable root must not cost the round the other roots' work.
        print(f"{root}: cannot read tree list ({str(exc)[-200:]})")
        act.record(
            settings,
            {
                "result": "root-unreadable",
                "root": str(root),
                "error": str(exc)[-300:],
                "avail_gib_start": round(round_.avail, 1),
                "used_pct_start": round_.pct,
            },
        )
        round_.tally["root-unreadable"] = round_.tally.get("root-unreadable", 0) + 1
        round_.judged[str(root)] = {"error": str(exc)[-200:]}
        return
    if not prefiltered:
        print(f"{root}: snapshot judge unavailable — the deletion gate decides every tree")
    round_.seen += len(rows)
    print(report.trees_line(root, rows))
    # meiva:note
    # The probe stamps every row with the sha of the file that produced it (312, 2026-11-03), so
    # the ledger says *which* judge ran, not just that one did. Drift then reads off the record
    # instead of needing a third process to compare two shas each round.
    round_.judged[str(root)] = {
        "trees": len(rows),
        "snapshot_judge": prefiltered,
        "probe_sha": (probe_line or {}).get("probe_sha"),
    }
    # meiva:note
    # Both directions of the surface, read-only: a registered tree whose directory is gone, and a
    # directory no registration knows about. Part of "look", so it runs with the gate closed too —
    # the night this is noticed should not be the night the disk is full.
    surface = evidence.surface_report(root)
    report.print_surface(surface)
    round_.surfaces.append(
        {
            "root": str(root),
            "registered": surface["registered"],
            "missing_dirs": len(surface["missing_dirs"]),
            "ghosts": surface["ghosts"],
            "foreign": len(surface["foreign"]),
            "not_a_tree": len(surface["not_a_tree"]),
        }
    )
    if not round_.gated:
        return
    # meiva:note
    # Repo-scoped evidence, on its own line, never a veto (405, 2026-11-03).
    stash = evidence.stash_report(root, rows)
    report.print_stash([stash])
    round_.stashes.append({key: stash.get(key) for key in ("root", "count", "oldest_days", "debt")})
    # meiva:note
    # A blind judge is a fault, not a licence — but "no snapshot judge" is only a fault where one
    # is expected (the probe's own repo). For every other root the deletion gate *is* the
    # judgement, by design: treating that as blindness made the runtime tree reclaimable only by
    # hand (2026-11-03, 312 asked).
    blind_stop = expects_snapshot(settings, root) and not prefiltered and not args.allow_blind_judge
    if blind_stop:
        print(f"{root}: reporting only — pass --allow-blind-judge to act on the gate alone")
    candidates = [r for r in rows if r.get("verdict") == "reclaimable"] if prefiltered else rows
    round_.candidates += len(candidates)
    if not candidates:
        print(f"{root}: nothing reclaimable this round")
        return
    reclaim_candidates(settings, root, args, round_, candidates, blind_stop)


def expects_snapshot(settings: Settings, root) -> bool:
    # meiva:note
    # Only the probe's own repo has a snapshot judge to expect; every other root is judged by the
    # deletion gate alone.
    return os.path.realpath(root) == os.path.realpath(settings.repo)


def reclaim_candidates(settings: Settings, root, args, round_, candidates, blind_stop):
    done = 0
    for row in candidates:
        if done >= args.max:
            print(f"hit --max {args.max} on {root}; the rest is left for the next round")
            break
        entry = act.reclaim(
            settings,
            str(root),
            row,
            args.dry_run or blind_stop,
            args.merge_cooloff_min,
            args.commit_cooloff_min,
        )
        entry.update({"avail_gib_start": round(round_.avail, 1), "used_pct_start": round_.pct})
        act.record(settings, entry)
        key = entry.get("blocked_by") or entry["result"]
        round_.tally[key] = round_.tally.get(key, 0) + 1
        print(report.tree_line(entry))
        if entry["result"] == "reclaimed":
            round_.freed += float(entry.get("freed_gib") or 0.0)
        if entry["result"] in ("reclaimed", "would-reclaim"):
            done += 1


def finish(settings: Settings, args, round_):
    if not round_.gated:
        return record_comfortable(settings, round_)
    if not round_.candidates:
        act.record(
            settings,
            {
                "result": "skip-no-candidates",
                "avail_gib": round(round_.avail, 1),
                "trees": round_.seen,
                "used_pct": round_.pct,
                "roots": round_.roots,
            },
        )
        return 0
    # meiva:note
    # Two readings, named: `avail_gib_start` is the one sample every per-tree line carries (it
    # decides nothing after the gate opens), so the round books its own before/after — ten
    # minutes of a shared machine moves the free space more than a 2 GiB tree does.
    avail_end, pct_end = evidence.disk_state(settings)
    act.record(
        settings,
        {
            "result": "run-summary",
            "candidates": round_.candidates,
            "tally": round_.tally,
            "avail_gib_start": round(round_.avail, 1),
            "used_pct_start": round_.pct,
            "avail_gib_end": round(avail_end, 1),
            "used_pct_end": pct_end,
            "dry_run": args.dry_run,
            "roots": round_.roots,
            "stashes": round_.stashes,
            "surfaces": round_.surfaces,
            "freed_gib": round_.freed,
        },
    )
    print(report.tally_line(round_.tally))
    print(f"total-freed: {round_.freed:.2f} GiB")
    return 0


def record_comfortable(settings: Settings, round_):
    avail_end, pct_end = evidence.disk_state(settings)
    act.record(
        settings,
        {
            "result": "skip-disk-comfortable",
            "avail_gib_start": round(round_.avail, 1),
            "used_pct_start": round_.pct,
            "avail_gib_end": round(avail_end, 1),
            "used_pct_end": pct_end,
            "roots": round_.roots,
            "judge": round_.judged,
            "surfaces": round_.surfaces,
        },
    )
    print(f"judge: {json.dumps(round_.judged, ensure_ascii=False)}")
    print("total-freed: 0.00 GiB")
    return 0


def settings_from(args) -> Settings:
    # meiva:note
    # Every default is the literal this tool used before the flags existed, so one copy runs
    # against the live home and another against a fixture without a second code path. `~` is
    # expanded here and nowhere below.
    repo = Path(args.repo).expanduser()
    ws = Path(args.ws).expanduser()
    return Settings(
        repo=repo,
        roots=tuple(Path(root).expanduser() for root in (args.roots or [repo])),
        probe=Path(args.probe).expanduser() if args.probe else None,
        probe_ledger=Path(args.probe_ledger).expanduser(),
        hold=Path(args.hold).expanduser(),
        manifests=Path(args.manifests).expanduser(),
        ledger=Path(args.ledger).expanduser() if args.ledger else ws / "worktree-gc-ledger.jsonl",
        never=Path(args.never).expanduser() if args.never else ws / "worktree-gc-never.txt",
        blind_state=Path(args.blind_state).expanduser()
        if args.blind_state
        else ws / "worktree-gc-blind-alert.json",
        home_env=Path(args.home_env).expanduser(),
        owner=args.owner,
        gateway=args.gateway,
    )


def build_parser():
    # meiva:note
    # Every path and threshold a schedule might need to move is a flag with an environment
    # fallback, because the same program judges a live volume and a fixture one.
    parser = argparse.ArgumentParser(
        description="Reclaim merged, clean, cool-off-passed worktrees under disk pressure."
    )
    parser.add_argument("--dry-run", action="store_true", help="say what would go; touch nothing")
    parser.add_argument("--trigger-avail-gib", type=float, default=AVAIL_TRIGGER_GIB)
    parser.add_argument("--trigger-used-pct", type=float, default=USED_PCT_TRIGGER)
    parser.add_argument(
        "--max", type=int, default=MAX_PER_RUN, help="most trees to reclaim per root in one run"
    )
    parser.add_argument(
        "--allow-blind-judge",
        action="store_true",
        help="delete on the deletion gate alone when the snapshot probe "
        "sees nothing; without it such a round only reports",
    )
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        metavar="DIR",
        help="checkout to sweep; repeatable (default: the repo itself)",
    )
    parser.add_argument("--merge-cooloff-min", type=float, default=MERGE_COOLOFF_MIN)
    parser.add_argument("--commit-cooloff-min", type=float, default=COMMIT_COOLOFF_MIN)
    parser.add_argument(
        "--report-hours",
        type=float,
        default=0.0,
        help="skip the sweep; print the skip-reason tally of the last N hours",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("WORKTREE_GC_REPO", Path("~/Meiva").expanduser()),
        help="shared checkout: the default root and the `gh` workspace",
    )
    parser.add_argument(
        "--probe",
        default=os.environ.get(
            "WORKTREE_GC_PROBE",
            Path("~/.meiva/workspaces/312/guards/worktree_pr_state.py").expanduser(),
        ),
        help="snapshot judge; empty string means this run has none",
    )
    parser.add_argument(
        "--probe-ledger",
        default=os.environ.get(
            "WORKTREE_GC_PROBE_LEDGER",
            Path("~/.meiva/workspaces/312/guards/worktree-states.jsonl").expanduser(),
        ),
        help="the judge's own ledger: the only thing this tool reads from it",
    )
    parser.add_argument(
        "--ws",
        default=os.environ.get("WORKTREE_GC_WS", Path("~/.meiva/workspaces/1818/ws").expanduser()),
        help="directory that holds the ledger, the never-list and the blind state",
    )
    parser.add_argument(
        "--ledger", default=None, help="decision ledger (default: <ws>/worktree-gc-ledger.jsonl)"
    )
    parser.add_argument(
        "--never", default=None, help="off-limits list (default: <ws>/worktree-gc-never.txt)"
    )
    parser.add_argument(
        "--blind-state",
        default=None,
        help="blind-alert state (default: <ws>/worktree-gc-blind-alert.json)",
    )
    parser.add_argument(
        "--hold",
        default=os.environ.get(
            "WORKTREE_GC_HOLD",
            Path("~/.meiva/workspaces/312/guards/worktree_hold.json").expanduser(),
        ),
        help="another agent's hold file; a held tree is never reclaimed",
    )
    parser.add_argument(
        "--manifests",
        default=os.environ.get(
            "WORKTREE_GC_MANIFESTS", Path("~/.meiva/run/meiva-root/manifests.json").expanduser()
        ),
        help="root supervisor manifests, read for the bare-sender token",
    )
    parser.add_argument(
        "--home-env",
        default=os.environ.get("WORKTREE_GC_HOME_ENV", Path("~/.meiva/.env").expanduser()),
        help="the home's .env, read for the gateway address when the flags do not name one",
    )
    parser.add_argument("--owner", type=int, default=1818, help="agent woken by the blind alert")
    parser.add_argument(
        "--gateway",
        default=os.environ.get("WORKTREE_GC_GATEWAY", ""),
        help="gateway base URL for alerts (default: the home's configured one)",
    )
    return parser
