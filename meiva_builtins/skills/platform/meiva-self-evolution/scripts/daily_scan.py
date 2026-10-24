#!/usr/bin/env python3
# meiva:note
# `daily_scan.py` is the shell of the daily scan: it collects the window through `collect.py`, writes the dataset and the full report, and prints the compact view. `scan_report.py` holds every decision about what the report says and whether the scan alerts, over values only; the coverage floor and the clock reading are read once by `process_env` and travel in `ScanEnv`. `main` returns the exit code, which `__main__` raises: `scan_report.alert_verdict` decides it in one order (empty window, low coverage, regression), and `1` is a hard failure of the arguments.
# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/coverage-sentinel.meiva.okf.md
"""Daily incremental self-evolution scan.

Collects the past N days (default 1) of real agent runs into a trace
dataset — the same JSONL records the weekly collect builds — writes the
full report beside it (`<date>.report.txt`), prints a compact stdout view
ending with a summary + pointer to that report, and exits 2 (ALERT) when any
run is worth mining.

Exit codes:
    0   no bad runs — nothing to act on
    2   ALERT — failed/fumbled runs found, or an empty dataset that the
        TEST- filter cannot explain (data source outage); the schedule wakes
        the self-evolution agent with this report
    1   hard failure (bad args, DB unreachable) — the schedule logs it
        and wakes the agent to investigate

A window whose only activity was TEST- prefixed spawns exits 0: the filter
removed every run by design, which is a quiet day, not an outage (QA review
of PR #698, 2026-08-29). The empty-dataset sentinel keys on pre-filter
activity — the source produced runs, the filter just dropped them all — so
the two cases stay distinguishable.

The alert threshold is deliberately low (one bad run alerts): the scan
wakes the self-evolution agent, not the user, so a false positive costs
one cheap review, while a missed bad run loses the earliest signal.

Run it like its sibling collect.py:

    .venv/bin/python skills/meiva-self-evolution/scripts/daily_scan.py --days 1
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from base.db import Database

# `collect` is a sibling in this skill's own scripts/ dir,
# not an importable package (PYTHONSAFEPATH=1 keeps a script's own directory
# off sys.path) — restore it for the sibling import. Structure Rule 6
# recognizes this exact __file__-derived, within-skill shape as the endorsed
# guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect
import scan_report
from events_window import resolve_window, week_label

from base.host.atomic_io import write_text_atomic


class ScanResult(NamedTuple):
    """One scan's output."""

    records: list[dict[str, Any]]
    path: Path
    counts: dict[str, int]


# meiva:note
# Collect the window's runs and write them under `$MEIVA_HOME/self_evolution/daily/<week>.jsonl`;
# `week` is the label the caller names the dataset by. The weekly `dataset/` files are never
# touched, so a daily run can never clobber a weekly file that shares the same date stamp.
#
# `counts` reports how many window agents the source produced and how many the TEST- filter
# dropped (`{"seen", "excluded_test", "skipped_meta"}` from collect.collect_with_counts): the
# empty-dataset sentinel keys on it to tell a TEST-only window from a broken data source.
#
# Source: the gateway's /api/events; a failed read fails the scan.
def scan(
    days: int,
    week: str,
    *,
    env: collect.CollectFacts,
    database: Database,
    include_test: bool = False,
) -> ScanResult:
    records, counts = collect.collect_with_counts(
        resolve_window(env.now, days, None, None),
        week,
        database=database,
        gateway=env.gateway,
        include_test=include_test,
    )
    out_dir = env.home / "self_evolution" / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = collect.write_records(records, out_dir / f"{week}.jsonl")
    return ScanResult(records, path, counts)


# meiva:note
# What the daily scan reads once at its start: the collection env, and the judgeable share below which the scan alerts.
@dataclass(frozen=True)
class ScanEnv:
    collection: collect.CollectFacts
    coverage_floor: float


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Daily incremental self-evolution scan — collect the past day's runs, "
        "report, alert (exit 2) on bad runs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--days", type=int, default=1, help="window size in days (default 1)")
    p.add_argument(
        "--include-test",
        action="store_true",
        help="include TEST- prefixed benchmark/probe spawns (measurement only; default excludes)",
    )
    return p.parse_args(list(argv))


# meiva:note
# Run the scan and return its exit code. The full report is published atomically (a unique temporary file, then a rename), so a reader never sees a partial report and a failed rerun leaves the previous file in place; when it cannot be written the compact view is not used, because the rich lines are then the only copy of the details.
def main(argv: Sequence[str], env: ScanEnv, *, database: Database) -> int:
    args = _parse_args(argv)
    if args.days < 1:
        print("error: --days must be >= 1", file=sys.stderr)
        return 1
    result = scan(
        args.days,
        week_label(env.collection.now),
        env=env.collection,
        database=database,
        include_test=args.include_test,
    )
    view = scan_report.ScanView(
        result.path, args.days, result.counts, env.collection.now, env.coverage_floor
    )
    report_path: Path | None = result.path.with_suffix(".report.txt")
    try:
        write_text_atomic(
            result.path.with_suffix(".report.txt"),
            scan_report.render(result.records, view),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"warning: could not write full report {report_path}: {exc}", file=sys.stderr)
        report_path = None
    print(scan_report.render(result.records, view, report_path=report_path, compact_bad=True))
    return scan_report.alert_verdict(result.records, result.counts, env.coverage_floor).code


# meiva:note
# The composition root of the standalone scan: the collection env and the configured coverage floor.
def process_env() -> ScanEnv:
    from base.config import settings

    return ScanEnv(
        collection=collect.process_facts(),
        coverage_floor=settings.general.self_evolution_coverage_alert_floor,
    )


if __name__ == "__main__":
    _parse_args(sys.argv[1:])
    raise SystemExit(main(sys.argv[1:], process_env(), database=collect.process_database()))
