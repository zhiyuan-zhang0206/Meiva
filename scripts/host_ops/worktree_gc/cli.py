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
from collections.abc import Sequence
from pathlib import Path

from base.host.boundary.options import options

from . import act, evidence, report
from .contracts import (
    AVAIL_TRIGGER_GIB,
    COMMIT_COOLOFF_MIN,
    MAX_PER_RUN,
    MERGE_COOLOFF_MIN,
    USED_PCT_TRIGGER,
    Settings,
    TreeRow,
)
from .values import float_of, text_of

# meiva:note
# The four stash fields the round summary carries; the per-entry detail stays in the round's
# printed line, so the ledger records the state of the repo, not a dump of it.
STASH_FIELDS = ("root", "count", "oldest_days", "debt")


@dataclasses.dataclass(frozen=True)
class Options:
    # meiva:note
    # The command line, read once into named fields. Every default is the literal this tool used
    # before the flags existed, so one copy runs against the live home and another against a
    # fixture without a second code path; `base.host.boundary.options` is the one seam where
    # argparse's `Any` stops.
    dry_run: bool
    trigger_avail_gib: float
    trigger_used_pct: float
    max: int
    allow_blind_judge: bool
    roots: list[str] | None
    # meiva:note
    # An int default and a `type=float` flag share these two: the checker compares against
    # `isinstance`, where `30` is not a `float`, so both forms are declared.
    merge_cooloff_min: float | int
    commit_cooloff_min: float | int
    report_hours: float
    repo: str
    probe: str
    probe_ledger: str
    ws: str
    ledger: str | None
    never: str | None
    blind_state: str | None
    hold: str
    manifests: str
    home_env: str
    owner: int
    gateway: str


@dataclasses.dataclass
class Round:
    # meiva:note
    # One run's readings and tallies, so no function has to carry nine counters in its signature.
    avail: float = 0.0
    pct: float = 0.0
    gated: bool = False
    tally: dict[str, int] = dataclasses.field(default_factory=dict)
    judged: dict[str, dict[str, object]] = dataclasses.field(default_factory=dict)
    surfaces: list[dict[str, object]] = dataclasses.field(default_factory=list)
    stashes: list[dict[str, object]] = dataclasses.field(default_factory=list)
    roots: list[str] = dataclasses.field(default_factory=list)
    seen: int = 0
    candidates: int = 0
    freed: float = 0.0


def main(argv: Sequence[str] | None = None) -> int:
    parsed = options(build_parser().parse_args(argv), Options)
    settings = settings_from(parsed)
    if parsed.report_hours:
        return act.ledger_report(settings, parsed.report_hours)
    round_ = open_round(settings, parsed)
    for root in settings.roots:
        sweep_root(settings, root, parsed, round_)
    return finish(settings, parsed, round_)


def open_round(settings: Settings, parsed: Options) -> Round:
    round_ = Round(roots=[str(root) for root in settings.roots])
    round_.avail, round_.pct = evidence.disk_state(settings)
    round_.gated = round_.avail < parsed.trigger_avail_gib or round_.pct >= parsed.trigger_used_pct
    print(
        report.banner(
            round_.avail,
            round_.pct,
            parsed.trigger_avail_gib,
            parsed.trigger_used_pct,
            gated=round_.gated,
        )
    )
    return round_


def sweep_root(settings: Settings, root: Path, parsed: Options, round_: Round) -> None:
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
        "probe_sha": probe_line.get("probe_sha"),
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
    round_.stashes.append({key: stash.get(key) for key in STASH_FIELDS})
    # meiva:note
    # A blind judge is a fault, not a licence — but "no snapshot judge" is only a fault where one
    # is expected (the probe's own repo). For every other root the deletion gate *is* the
    # judgement, by design: treating that as blindness made the runtime tree reclaimable only by
    # hand (2026-11-03, 312 asked).
    blind_stop = (
        expects_snapshot(settings, root) and not prefiltered and not parsed.allow_blind_judge
    )
    if blind_stop:
        print(f"{root}: reporting only — pass --allow-blind-judge to act on the gate alone")
    candidates = [r for r in rows if r.get("verdict") == "reclaimable"] if prefiltered else rows
    round_.candidates += len(candidates)
    if not candidates:
        print(f"{root}: nothing reclaimable this round")
        return
    reclaim_candidates(settings, root, parsed, round_, candidates, blind_stop=blind_stop)


def expects_snapshot(settings: Settings, root: str | Path) -> bool:
    # meiva:note
    # Only the probe's own repo has a snapshot judge to expect; every other root is judged by the
    # deletion gate alone.
    return os.path.realpath(root) == os.path.realpath(settings.repo)


def reclaim_candidates(
    settings: Settings,
    root: Path,
    parsed: Options,
    round_: Round,
    candidates: Sequence[TreeRow],
    *,
    blind_stop: bool,
) -> None:
    done = 0
    for row in candidates:
        if done >= parsed.max:
            print(f"hit --max {parsed.max} on {root}; the rest is left for the next round")
            break
        entry = act.reclaim(
            settings,
            str(root),
            row,
            dry_run=parsed.dry_run or blind_stop,
            merge_cooloff_min=parsed.merge_cooloff_min,
            commit_cooloff_min=parsed.commit_cooloff_min,
        )
        entry.update({"avail_gib_start": round(round_.avail, 1), "used_pct_start": round_.pct})
        act.record(settings, entry)
        key = text_of(entry.get("blocked_by")) or text_of(entry.get("result"))
        round_.tally[key] = round_.tally.get(key, 0) + 1
        print(report.tree_line(entry))
        if entry["result"] == "reclaimed":
            round_.freed += float_of(entry.get("freed_gib"))
        if entry["result"] in ("reclaimed", "would-reclaim"):
            done += 1


def finish(settings: Settings, parsed: Options, round_: Round) -> int:
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
            "dry_run": parsed.dry_run,
            "roots": round_.roots,
            "stashes": round_.stashes,
            "surfaces": round_.surfaces,
            "freed_gib": round_.freed,
        },
    )
    print(report.tally_line(round_.tally))
    print(f"total-freed: {round_.freed:.2f} GiB")
    return 0


def record_comfortable(settings: Settings, round_: Round) -> int:
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


def settings_from(parsed: Options) -> Settings:
    # meiva:note
    # Every path is resolved once, before a round starts: one copy of this tool runs against the
    # live home, another against a fixture, and the difference is this value alone. `~` is
    # expanded here and nowhere below.
    repo = Path(parsed.repo).expanduser()
    ws = Path(parsed.ws).expanduser()
    return Settings(
        repo=repo,
        roots=tuple(Path(root).expanduser() for root in (parsed.roots or [repo])),
        probe=Path(parsed.probe).expanduser() if parsed.probe else None,
        probe_ledger=Path(parsed.probe_ledger).expanduser(),
        hold=Path(parsed.hold).expanduser(),
        manifests=Path(parsed.manifests).expanduser(),
        ledger=Path(parsed.ledger).expanduser()
        if parsed.ledger
        else ws / "worktree-gc-ledger.jsonl",
        never=Path(parsed.never).expanduser() if parsed.never else ws / "worktree-gc-never.txt",
        blind_state=Path(parsed.blind_state).expanduser()
        if parsed.blind_state
        else ws / "worktree-gc-blind-alert.json",
        home_env=Path(parsed.home_env).expanduser(),
        owner=parsed.owner,
        gateway=parsed.gateway,
    )


def build_parser() -> argparse.ArgumentParser:
    # meiva:note
    # Every path and threshold a schedule might need to move is a flag with an environment
    # fallback, because the same program judges a live volume and a fixture one. A default that
    # is a path is written as a string: the flags are declared `str` above, so a `Path` here
    # would be a shape the dataclass refuses.
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
        default=os.environ.get("WORKTREE_GC_REPO", str(Path("~/Meiva").expanduser())),
        help="shared checkout: the default root and the `gh` workspace",
    )
    parser.add_argument(
        "--probe",
        default=os.environ.get(
            "WORKTREE_GC_PROBE",
            str(Path("~/.meiva/workspaces/312/guards/worktree_pr_state.py").expanduser()),
        ),
        help="snapshot judge; empty string means this run has none",
    )
    parser.add_argument(
        "--probe-ledger",
        default=os.environ.get(
            "WORKTREE_GC_PROBE_LEDGER",
            str(Path("~/.meiva/workspaces/312/guards/worktree-states.jsonl").expanduser()),
        ),
        help="the judge's own ledger: the only thing this tool reads from it",
    )
    parser.add_argument(
        "--ws",
        default=os.environ.get(
            "WORKTREE_GC_WS", str(Path("~/.meiva/workspaces/1818/ws").expanduser())
        ),
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
            str(Path("~/.meiva/workspaces/312/guards/worktree_hold.json").expanduser()),
        ),
        help="another agent's hold file; a held tree is never reclaimed",
    )
    parser.add_argument(
        "--manifests",
        default=os.environ.get(
            "WORKTREE_GC_MANIFESTS",
            str(Path("~/.meiva/run/meiva-root/manifests.json").expanduser()),
        ),
        help="root supervisor manifests, read for the bare-sender token",
    )
    parser.add_argument(
        "--home-env",
        default=os.environ.get("WORKTREE_GC_HOME_ENV", str(Path("~/.meiva/.env").expanduser())),
        help="the home's .env, read for the gateway address when the flags do not name one",
    )
    parser.add_argument("--owner", type=int, default=1818, help="agent woken by the blind alert")
    parser.add_argument(
        "--gateway",
        default=os.environ.get("WORKTREE_GC_GATEWAY", ""),
        help="gateway base URL for alerts (default: the home's configured one)",
    )
    return parser
