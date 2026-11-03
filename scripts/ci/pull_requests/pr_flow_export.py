# meiva:note
# PR flow exporter — samples the Meiva repo's merge flow into OTLP gauges.
#
# Runs once a day on macmini (the registered machine that holds the `gh`
# session), recomputes the trailing window of the fleet's pull-request
# pipeline, and publishes it to Prometheus + Loki through the unified telemetry
# pipeline as ``pr_flow_daily``: one event per complete day in the trailing
# window, re-emitted on every run so the whole window stays visible inside
# Prometheus's retention. Fields: ``merged_count`` plus (when the day has
# samples) ``ready_to_merge_median_seconds`` / ``ready_to_merge_p90_seconds``.
#
# Every numeric payload field is dispositioned as an ObservableGauge in
# ``base/telemetry/otlp/telemetry_otlp.py`` — the values are per-day absolute state, never
# sums, and re-emission must replace them rather than accrue them.
#
# Metric definitions (cluster-tz days; the fleet timezone is Asia/Shanghai):
#
# - **ready -> merged**: for PRs whose ``merged_at`` falls on day D,
#   ``merged_at - ready_at`` in seconds, where ``ready_at`` is the first
#   ``ready_for_review`` timeline event, else ``created_at`` (the fleet opens
#   PRs ready; the fallback keeps draft-style flows honest if one appears).
#   median / p90 over the day's PRs, linear interpolation between closest
#   ranks.
#
# Persistence (``$MEIVA_HOME/state/pr-flow/``, survives the prod source-tree
# resets that clean untracked files out of the checkout):
#
# - ``cache.json`` — per-PR compact records keyed by number, refreshed only
#   when GitHub's ``updated_at`` moved (incremental cache; the run re-fetches a
#   timeline only when the PR changed). This is the read path's whole write
#   surface besides the snapshot.
# - ``snapshot.json`` — the latest computed payload (window, days, run,
#   stats), rewritten atomically each run. The reconciliation surface: rerun
#   the script (``--print``) and diff against the emitted gauges.
#
# Read-only by construction: GitHub is read through ``gh api`` GETs; nothing
# here writes to GitHub.
#
# Rate-limit budget: a cold run fetches one list page per 100 closed PRs plus
# one timeline per PR (at most ``MAX_TIMELINE_PAGES`` pages each); steady
# state refetches only PRs whose ``updated_at`` moved since the last run
# (typically well under 100/day) plus ~24 list pages. Measured on 2026-09-14 (30-day window, 1361 merged
# PRs): a cold walk is ~1450 GitHub reads, steady state ~150-200/day — both
# comfortably inside the account's 5000/hour budget.
#
# Failure policy: the PR list is the authoritative population, so a failed
# list fetch aborts the run (exit 1, no emission — old Prometheus values stay).
# Timeline failures degrade to cached or partial records and are counted in
# ``stats``.
# meiva:link scripts/ci/docs/ci.meiva.okf.md


from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import TypedDict, TypeGuard
from zoneinfo import ZoneInfo

# Script-mode path guard (PYTHONSAFEPATH=1 removed the implicit script-dir
# entry): this checkout's root first so `base` and `scripts.ci` resolve against
# this tree.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# meiva:note
# The script-mode root guard must run before importing the checkout-owned API.
from base.host.boundary.fields import optional_int_field  # noqa: E402
from base.host.boundary.json_values import is_object_dict, is_object_list  # noqa: E402
from base.host.boundary.options import options  # noqa: E402
from base.host.boundary.untyped import object_of  # noqa: E402
from base.telemetry import EventPipeline  # noqa: E402
from scripts.ci.pull_requests.gh_process import GhProcess  # noqa: E402

DEFAULT_REPO = "zhiyuan-zhang0206/Meiva"
DEFAULT_WINDOW_DAYS = 30
PROCESS_NAME = "pr-flow"

_GH_TIMEOUT_S = 60.0
# Bounded pagination: the list caps this run's read budget; a truncated walk
# logs loudly rather than silently shrinking the window.
MAX_LIST_PAGES = 80
MAX_TIMELINE_PAGES = 3
# GitHub's per_page ceiling (100); every walk pages in batches of this size
# (task #3696 exception inventory).
_PAGE_SIZE = 100

_STATE_DIR_RELATIVE = Path("state") / "pr-flow"


# meiva:note
# One `gh api` row projected by jq: a closed PR's list row, or a timeline event.
GhRow = dict[str, object]
# meiva:note
# One day's figures: the merge count and, when the day has samples, the ready-to-merge seconds.
DayFigures = dict[str, int | float]


def _is_dict_list(value: object) -> TypeGuard[list[GhRow]]:
    return is_object_list(value) and all(is_object_dict(item) for item in value)


def _is_entry_map(value: object) -> TypeGuard[dict[str, dict[str, object]]]:
    return is_object_dict(value) and all(is_object_dict(entry) for entry in value.values())


class PrFlowError(RuntimeError):
    """A fetch that the run cannot proceed past (the PR list)."""


class SnapshotWindow(TypedDict):
    days: int
    start_day: str
    end_day: str
    timezone: str


class Snapshot(TypedDict):
    # meiva:note
    # The latest computed payload: window, per-day figures and the fetch accounting.

    version: int
    generated_at: str
    repo: str
    window: SnapshotWindow
    days: dict[str, DayFigures]
    stats: dict[str, int]


@dataclass(frozen=True)
class Options:
    # meiva:note
    # The flags `main` reads, as the parser answered them.

    repo: str
    window_days: int
    state_dir: Path | None
    dry_run: bool
    print_snapshot: bool


def _number(meta: Mapping[str, object]) -> int:
    number = optional_int_field(meta, "number")
    if number is None:
        raise PrFlowError("pull request row lacks an integer number")
    return number


@dataclass
class PrRecord:
    """One merged PR's pipeline record (the cache unit)."""

    number: int
    updated_at: str
    created_at: str
    merged_at: str
    ready_at: str
    partial: bool = False  # True when the timeline could not be read

    def to_cache(self) -> dict[str, str | bool]:
        return {
            "updated_at": self.updated_at,
            "created_at": self.created_at,
            "merged_at": self.merged_at,
            "ready_at": self.ready_at,
            "partial": self.partial,
        }

    @classmethod
    def from_cache(cls, number: int, data: Mapping[str, object]) -> PrRecord:
        return cls(
            number=number,
            updated_at=str(data.get("updated_at") or ""),
            created_at=str(data.get("created_at") or ""),
            merged_at=str(data.get("merged_at") or ""),
            ready_at=str(data.get("ready_at") or ""),
            partial=bool(data.get("partial")),
        )


@dataclass
class RunStats:
    """Fetch accounting for the snapshot and the PR description's bounds."""

    gh_api_calls: int = 0
    timeline_fetches: int = 0
    timeline_cache_hits: int = 0
    timeline_failures: int = 0

    def to_json(self) -> dict[str, int]:
        return {
            "gh_api_calls": self.gh_api_calls,
            "timeline_fetches": self.timeline_fetches,
            "timeline_cache_hits": self.timeline_cache_hits,
            "timeline_failures": self.timeline_failures,
        }


def cluster_tz() -> ZoneInfo:
    """The cluster's wall clock — the day boundary every aggregate uses."""
    from base.clock import Clock

    return Clock.from_settings().explicit_zone()


def window_days(now: datetime, days: int, tz: ZoneInfo) -> list[date]:
    """The last `days` complete cluster-tz days, oldest first.

    Day D is complete once D+1 starts; the run at 00:2x on D+1 aggregates
    D (and the whole trailing window) with D's data final — ``merged_at``
    stamps the day, so a completed day never gains merges afterwards.
    """
    today = now.astimezone(tz).date()
    last_complete = today - timedelta(days=1)
    return [last_complete - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def run_gh(args: list[str], *, run_process: GhProcess, timeout: float = _GH_TIMEOUT_S) -> str:
    """Run one gh command and return stdout; raise on any failure.

    Direct `gh` (not the API wrapper) because the job runs where the gh
    session lives; the callers project fields with --jq so no payload class
    is parsed whole.
    """
    result = run_process(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise PrFlowError(
            f"gh {' '.join(args[:4])} failed: {detail[-1] if detail else result.returncode}"
        )
    return result.stdout


def fetch_closed_prs(
    repo: str, since: datetime, stats: RunStats, *, run_process: GhProcess, max_pages: int
) -> list[GhRow]:
    """Closed PRs updated since `since`, newest-updated first (paginated).

    Sorted by ``updated`` desc, so the walk can stop at the first page whose
    tail predates the window. Only the fields the aggregates need are
    projected.
    """
    jq = "[.[] | {number, created_at, updated_at, merged_at}] | @json"
    prs: list[GhRow] = []
    for page in range(1, max_pages + 1):
        out = run_gh(
            [
                "api",
                f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc"
                f"&per_page={_PAGE_SIZE}&page={page}",
                "--jq",
                jq,
            ],
            run_process=run_process,
        )
        stats.gh_api_calls += 1
        batch = object_of(json.loads, out)
        if not _is_dict_list(batch):
            raise PrFlowError(f"unexpected pulls payload on page {page}")
        prs.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return prs
        oldest = min((str(item.get("updated_at") or "") for item in batch), default="")
        try:
            if oldest and datetime.fromisoformat(oldest) < since:
                return prs
        except ValueError:
            continue  # unparsable stamp: keep walking rather than stop early
    raise PrFlowError(
        f"pulls walk exceeded {max_pages} pages — run budget exhausted, window truncated"
    )


def fetch_timeline(
    repo: str, number: int, stats: RunStats, *, run_process: GhProcess, max_pages: int
) -> list[GhRow]:
    """One PR's issue timeline, projected to the events the aggregates read.

    Bounded at ``MAX_TIMELINE_PAGES`` pages; a busier timeline is truncated
    (logged via stats) rather than unbounded.
    """
    jq = "[.[] | {event, created_at}] | @json"
    events: list[GhRow] = []
    for page in range(1, max_pages + 1):
        out = run_gh(
            [
                "api",
                f"repos/{repo}/issues/{number}/timeline?per_page={_PAGE_SIZE}&page={page}",
                "--jq",
                jq,
            ],
            run_process=run_process,
        )
        stats.gh_api_calls += 1
        batch = object_of(json.loads, out)
        if not _is_dict_list(batch):
            raise PrFlowError(f"unexpected timeline payload for PR #{number}")
        events.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
    else:
        print(
            f"pr-flow: PR #{number} timeline truncated at {max_pages} pages",
            file=sys.stderr,
        )
    return events


def build_record(meta: Mapping[str, object], events: Sequence[Mapping[str, object]]) -> PrRecord:
    """The compact per-PR record from one PR's list row + timeline."""
    number = _number(meta)
    created_at = str(meta.get("created_at") or "")
    ready_at = created_at
    for event in events:
        if event.get("event") == "ready_for_review" and event.get("created_at"):
            ready_at = str(event["created_at"])
            break
    merged_at = str(meta.get("merged_at") or "")
    return PrRecord(
        number=number,
        updated_at=str(meta.get("updated_at") or ""),
        created_at=created_at,
        merged_at=merged_at,
        ready_at=ready_at,
    )


def percentile(sorted_values: list[float], quantile: float) -> float:
    """Linear interpolation between closest ranks (numpy's default method).

    n == 1 returns the single value; the caller only passes non-empty lists.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = quantile * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def duration_seconds(record: PrRecord) -> float | None:
    """ready -> merged seconds; None when missing/unparsable/non-positive.

    A zero-or-negative span is a data anomaly (a ready stamp after the merge
    stamp), not a real sample — it must not drag a percentile down.
    """
    if not record.ready_at or not record.merged_at:
        return None
    try:
        ready = datetime.fromisoformat(record.ready_at)
        merged = datetime.fromisoformat(record.merged_at)
    except ValueError:
        return None
    duration = (merged - ready).total_seconds()
    return duration if duration > 0 else None


# meiva:note
# The per-day metric payload for the trailing window. Days with no merges keep merged_count = 0 and
# omit the sample-based fields: an absent optional metric is not zero.
def compute_days(
    records: list[PrRecord],
    days: list[date],
    tz: ZoneInfo,
) -> dict[str, DayFigures]:
    by_day: dict[date, list[PrRecord]] = {day: [] for day in days}
    for record in records:
        if not record.merged_at:
            continue
        try:
            merged_day = datetime.fromisoformat(record.merged_at).astimezone(tz).date()
        except ValueError:
            continue
        if merged_day in by_day:
            by_day[merged_day].append(record)

    payload: dict[str, DayFigures] = {}
    for day in days:
        group = by_day[day]
        entry: DayFigures = {"merged_count": len(group)}
        durations = sorted(
            value for record in group if (value := duration_seconds(record)) is not None
        )
        if durations:
            entry["ready_to_merge_median_seconds"] = round(percentile(durations, 0.5), 1)
            entry["ready_to_merge_p90_seconds"] = round(percentile(durations, 0.9), 1)
        payload[day.isoformat()] = entry
    return payload


def load_cache(path: Path) -> dict[str, dict[str, object]]:
    """The per-PR cache; a missing or corrupt file reads as empty (heals)."""
    try:
        data = object_of(json.loads, path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("prs") if is_object_dict(data) else None
    return entries if _is_entry_map(entries) else {}


def save_json(path: Path, payload: Mapping[str, object]) -> None:
    """Atomic write (tmp + rename) so a crash never leaves a torn file; a
    failed rename keeps the previous file and clears the stranded tmp."""
    from base.host.atomic_io import write_text_atomic

    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path,
        json.dumps(payload, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
        sync_file=False,
    )


def collect_records(
    prs: Sequence[Mapping[str, object]],
    window: list[date],
    tz: ZoneInfo,
    repo: str,
    cache: Mapping[str, Mapping[str, object]],
    stats: RunStats,
    *,
    run_process: GhProcess,
) -> list[PrRecord]:
    """Merged-in-window PR records, refreshed only where `updated_at` moved.

    The cache is what bounds steady-state reads: an unchanged PR reuses its
    record. A changed PR refetches its timeline; a failed refetch falls back
    to the cached record when one exists (stale beats missing) and to a
    partial record otherwise — either way the failure is counted, never
    silently absorbed.
    """
    wanted = set(window)
    records: list[PrRecord] = []
    for meta in prs:
        merged_raw = _merged_in_window(meta, wanted, tz)
        if merged_raw is None:
            continue
        number = _number(meta)
        cached_raw = cache.get(str(number))
        cached = PrRecord.from_cache(number, cached_raw) if cached_raw else None
        if cached is not None and _cache_current(cached, meta):
            stats.timeline_cache_hits += 1
            records.append(cached)
            continue
        stats.timeline_fetches += 1
        records.append(_refresh_record(meta, merged_raw, cached, repo, stats, run_process))
    return records


def _merged_in_window(meta: Mapping[str, object], wanted: set[date], tz: ZoneInfo) -> str | None:
    """The PR's `merged_at` string when it merged on a day in the window, else None."""
    merged_raw = meta.get("merged_at")
    if not isinstance(merged_raw, str) or not merged_raw:
        return None
    try:
        merged_day = datetime.fromisoformat(merged_raw).astimezone(tz).date()
    except ValueError:
        return None
    return merged_raw if merged_day in wanted else None


def _cache_current(cached: PrRecord, meta: Mapping[str, object]) -> bool:
    return (
        not cached.partial
        and cached.updated_at == str(meta.get("updated_at") or "")
        and bool(cached.ready_at)
    )


def _refresh_record(
    meta: Mapping[str, object],
    merged_raw: str,
    cached: PrRecord | None,
    repo: str,
    stats: RunStats,
    run_process: GhProcess,
) -> PrRecord:
    number = _number(meta)
    try:
        events = fetch_timeline(
            repo, number, stats, run_process=run_process, max_pages=MAX_TIMELINE_PAGES
        )
    except (PrFlowError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        stats.timeline_failures += 1
        print(f"pr-flow: timeline fetch failed for PR #{number}: {exc}", file=sys.stderr)
        if cached is not None and cached.ready_at:
            return cached
        return PrRecord(
            number=number,
            updated_at=str(meta.get("updated_at") or ""),
            created_at=str(meta.get("created_at") or ""),
            merged_at=merged_raw,
            ready_at="",
            partial=True,
        )
    return build_record(meta, events)


def build_snapshot(
    *,
    repo: str,
    days: list[date],
    day_payload: dict[str, DayFigures],
    tz: ZoneInfo,
    stats: RunStats,
    merged_in_window: int,
    partial: int,
    now: datetime,
) -> Snapshot:
    return Snapshot(
        version=1,
        generated_at=now.astimezone(UTC).isoformat(),
        repo=repo,
        window=SnapshotWindow(
            days=len(days),
            start_day=days[0].isoformat(),
            end_day=days[-1].isoformat(),
            timezone=str(tz),
        ),
        days=day_payload,
        stats={
            "merged_in_window": merged_in_window,
            "partial_records": partial,
            **stats.to_json(),
        },
    )


# meiva:note
# Emits the daily events through the unified telemetry pipeline; dry_run keeps the seam silent.
def emit_snapshot(
    snapshot: Snapshot, *, dry_run: bool, pipeline: EventPipeline, home: Path
) -> None:
    if dry_run:
        return
    emit_events(snapshot, pipeline=pipeline, home=home)


# meiva:note
# The pipeline write, kept as its own seam so tests can assert dry-run silence.
def emit_events(snapshot: Snapshot, *, pipeline: EventPipeline, home: Path) -> None:
    from base import telemetry

    telemetry.init_telemetry(home=home, process=PROCESS_NAME, pipeline=pipeline)
    pipeline.warmup_exports()
    for day, entry in sorted(snapshot["days"].items()):
        telemetry.emit("telemetry", "pr_flow_daily", attributes={"day": day, **entry})

    telemetry.sync()


def main(argv: list[str], *, run_process: GhProcess, home: Path) -> int:
    parser = argparse.ArgumentParser(
        description="Sample the repository's pull-request merge flow into OTLP gauges."
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="owner/name to sample")
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help="trailing complete days to sample (default %(default)s)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="snapshot/cache directory (default $MEIVA_HOME/state/pr-flow)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="compute + print, no writes, no emit"
    )
    parser.add_argument(
        "--print",
        dest="print_snapshot",
        action="store_true",
        help="print the snapshot JSON to stdout",
    )
    args = options(parser.parse_args(argv), Options)
    if args.window_days < 1 or args.window_days > 90:
        parser.error("--window-days must be between 1 and 90")

    image = None
    if not args.dry_run:
        from base.native_process.loaded_commit import LoadedCommit

        image = LoadedCommit.capture(_REPO_ROOT)

    tz = cluster_tz()
    now = datetime.now(UTC)
    days = window_days(now, args.window_days, tz)
    since = datetime.combine(days[0], dtime(0, 0), tzinfo=tz).astimezone(UTC)
    stats = RunStats()

    prs = fetch_closed_prs(
        args.repo, since, stats, run_process=run_process, max_pages=MAX_LIST_PAGES
    )

    state_dir = args.state_dir if args.state_dir is not None else home / _STATE_DIR_RELATIVE
    cache = load_cache(state_dir / "cache.json")

    records = collect_records(prs, days, tz, args.repo, cache, stats, run_process=run_process)

    day_payload = compute_days(records, days, tz)
    snapshot = build_snapshot(
        repo=args.repo,
        days=days,
        day_payload=day_payload,
        tz=tz,
        stats=stats,
        merged_in_window=len(records),
        partial=sum(1 for record in records if record.partial),
        now=now,
    )

    if not args.dry_run:
        save_json(
            state_dir / "cache.json",
            {"version": 1, "prs": {str(r.number): r.to_cache() for r in records}},
        )
        save_json(state_dir / "snapshot.json", snapshot)
        from scripts.ci.pull_requests.export_process import owned_event_pipeline

        with owned_event_pipeline(PROCESS_NAME, image=image, home=home) as pipeline:
            emit_snapshot(snapshot, dry_run=False, pipeline=pipeline, home=home)

    if args.print_snapshot:
        print(json.dumps(snapshot, indent=1, sort_keys=True))
    else:
        window = snapshot["window"]
        print(
            f"pr-flow: {window['start_day']}..{window['end_day']} — "
            f"{snapshot['stats']['merged_in_window']} merged PRs, "
            f"{stats.timeline_fetches} timeline fetches, "
            f"{stats.timeline_cache_hits} cache hits, "
            f"{'dry-run (no writes, no emit)' if args.dry_run else 'emitted'}"
        )
    return 0


if __name__ == "__main__":
    from base.paths import meiva_home

    sys.exit(main(sys.argv[1:], run_process=subprocess.run, home=meiva_home()))
