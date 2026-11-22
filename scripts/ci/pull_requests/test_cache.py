# meiva:link docs/conventions/engineering/test-selection.md

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from meizel.api import lanes_at

_DEFAULT_MIN_SELECTED_COUNT = 150
_DEFAULT_CANARY_PERCENT = 5


class _CliArgs(argparse.Namespace):
    command: str = ""
    event: str = ""
    backend: str = ""
    mode: str = ""
    decision: str = ""
    selected_count: str = "0"
    pr_cache_hit: str = "false"
    run_id: str = ""
    run_attempt: str = "1"
    min_selected_count: int = _DEFAULT_MIN_SELECTED_COUNT
    canary_percent: int = _DEFAULT_CANARY_PERCENT
    github_output: str | None = None
    input_path: str = ""
    out: str | None = None
    repo: str = "."
    plan: str = ""
    durations: str | None = None
    group: int = 1
    groups: int = 1
    report: str | None = None
    skip_enabled: str = "false"
    canary: str = "false"
    recorded: int = 0
    label: str = ""


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast("list[object]", value)]


def _as_mapping_len(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    return len(cast("dict[object, object]", value))


def _load_json_object(path: Path) -> dict[str, object] | None:
    raw: object = cast("object", json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        return None
    return {str(key): val for key, val in cast("dict[object, object]", raw).items()}


def is_canary_run(
    event: str,
    run_id: str,
    run_attempt: str,
    *,
    canary_percent: int = _DEFAULT_CANARY_PERCENT,
) -> bool:
    if event != "pull_request" or canary_percent <= 0:
        return False
    if canary_percent >= 100:
        return True
    digest = hashlib.sha256(f"{run_id}:{run_attempt}".encode()).hexdigest()
    return int(digest, 16) % 100 < canary_percent


def decide_cache_mode(
    *,
    event: str,
    backend: str,
    mode: str,
    decision: str,
    selected_count: int,
    pr_cache_hit: str,
    run_id: str,
    run_attempt: str,
    min_selected_count: int = _DEFAULT_MIN_SELECTED_COUNT,
    canary_percent: int = _DEFAULT_CANARY_PERCENT,
) -> tuple[bool, bool]:
    canary = is_canary_run(event, run_id, run_attempt, canary_percent=canary_percent)
    skip_enabled = (
        event == "pull_request"
        and backend == "true"
        and mode == "enforce"
        and pr_cache_hit == "true"
        and (decision != "SELECTED" or selected_count >= min_selected_count)
        and not canary
    )
    return canary, skip_enabled


def filter_static_tests(paths: Sequence[str], static_lane: frozenset[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for raw in paths:
        path = raw.strip()
        if path and path in static_lane and path not in seen:
            seen.add(path)
            kept.append(path)
    return kept


def partition_targets(
    targets: Sequence[str],
    durations: Mapping[str, float],
    *,
    groups: int,
) -> list[list[str]]:
    if groups < 1:
        raise ValueError(f"groups must be >= 1, got {groups}")
    unique = sorted(set(targets))
    known = [float(v) for v in durations.values() if isinstance(v, (int, float)) and v >= 0.0]
    fallback = (sum(known) / len(known)) if known else 1.0

    def weight(path: str) -> float:
        raw = durations.get(path)
        if isinstance(raw, (int, float)) and raw >= 0.0:
            return float(raw)
        return fallback

    ordered = sorted(unique, key=lambda path: (-weight(path), path))
    bins: list[list[str]] = [[] for _ in range(groups)]
    loads: list[float] = [0.0] * groups
    for path in ordered:
        best = min(range(groups), key=lambda idx: (loads[idx], len(bins[idx]), idx))
        bins[best].append(path)
        loads[best] += weight(path)
    return [sorted(bucket) for bucket in bins]


def _format_active_summary(
    *,
    skippable: Sequence[str],
    run_count: int,
    report: Mapping[str, object] | None,
    durations: Mapping[str, float],
    recorded: int,
    uncacheable: int,
    unbounded: int,
    label: str,
) -> str:
    saved = sum(float(durations.get(path, 0.0)) for path in skippable)
    if report is not None:
        report_saved = report.get("saved_seconds")
        if isinstance(report_saved, (int, float)):
            saved = float(report_saved)
    lines = [
        f"### Test result cache ({label or 'active skip'})",
        f"- skipped: {len(skippable)}",
        f"- ran: {run_count}",
        f"- seconds saved (by `.test_durations`): {saved:.0f}",
        f"- recorded: {recorded}",
        f"- uncacheable: {uncacheable}; keyed by the whole tree: {unbounded}",
    ]
    return "\n".join(lines) + "\n"


def _format_shadow_summary(
    *,
    skippable_count: int,
    report: Mapping[str, object],
    canary: bool,
    recorded: int,
    uncacheable: int,
    unbounded: int,
    label: str,
) -> str:
    mode_label = label or ("canary" if canary else "shadow")
    would_skip = report.get("would_skip", skippable_count)
    would_skip_passed = report.get("would_skip_passed", 0)
    failed_list = _as_str_list(report.get("would_skip_failed"))
    not_run_count = len(_as_str_list(report.get("would_skip_not_run")))
    raw_saved = report.get("saved_seconds", 0.0)
    saved_val = float(raw_saved) if isinstance(raw_saved, (int, float)) else 0.0
    report_uncacheable = report.get("uncacheable", uncacheable)
    report_unbounded = report.get("unbounded", unbounded)
    lines = [
        f"### Test result cache ({mode_label})",
        f"- would skip: {would_skip} ({would_skip_passed} passed when run)",
        f"- would skip but failed: {len(failed_list)} {failed_list}",
        f"- would skip, not run: {not_run_count}",
        f"- seconds saved (by `.test_durations`): {saved_val:.0f}",
        f"- recorded: {recorded}",
        f"- uncacheable: {report_uncacheable}; keyed by the whole tree: {report_unbounded}",
    ]
    return "\n".join(lines) + "\n"


def format_summary(
    *,
    plan: Mapping[str, object],
    report: Mapping[str, object] | None,
    durations: Mapping[str, float],
    skip_enabled: bool,
    canary: bool,
    recorded: int,
    label: str = "",
) -> str:
    skippable = _as_str_list(plan.get("skippable"))
    run_list = _as_str_list(plan.get("run"))
    uncacheable = _as_mapping_len(plan.get("uncacheable"))
    unbounded = _as_mapping_len(plan.get("unbounded"))
    if skip_enabled:
        return _format_active_summary(
            skippable=skippable,
            run_count=len(run_list),
            report=report,
            durations=durations,
            recorded=recorded,
            uncacheable=uncacheable,
            unbounded=unbounded,
            label=label,
        )
    return _format_shadow_summary(
        skippable_count=len(skippable),
        report=report or {},
        canary=canary,
        recorded=recorded,
        uncacheable=uncacheable,
        unbounded=unbounded,
        label=label,
    )


def _parse_int(raw: str, *, default: int = 0) -> int:
    text = raw.strip()
    if not text:
        return default
    return int(text)


def _load_durations(path: Path | None) -> dict[str, float]:
    if path is None or not path.is_file():
        return {}
    raw = _load_json_object(path)
    if raw is None:
        return {}
    return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float))}


def _cmd_decide(args: _CliArgs) -> int:
    selected_count = _parse_int(args.selected_count, default=0)
    canary, skip_enabled = decide_cache_mode(
        event=args.event,
        backend=args.backend,
        mode=args.mode,
        decision=args.decision,
        selected_count=selected_count,
        pr_cache_hit=args.pr_cache_hit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        min_selected_count=args.min_selected_count,
        canary_percent=args.canary_percent,
    )
    canary_str = "true" if canary else "false"
    skip_str = "true" if skip_enabled else "false"
    payload = f"canary={canary_str}\nskip_enabled={skip_str}\n"
    if args.github_output is not None:
        out_path = Path(args.github_output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as handle:
            _ = handle.write(payload)
        print(
            f"test-cache decide: event={args.event} backend={args.backend} mode={args.mode} "
            f"decision={args.decision} selected_count={selected_count} "
            f"pr_cache_hit={args.pr_cache_hit} canary={canary_str} skip_enabled={skip_str}"
        )
    else:
        _ = sys.stdout.write(payload)
    return 0


def _cmd_filter_static(args: _CliArgs) -> int:
    if args.out is None:
        raise SystemExit("--out is required")
    repo_root = Path(args.repo).resolve()
    static_lane = frozenset(lanes_at(repo_root).static)
    raw_lines = Path(args.input_path).read_text(encoding="utf-8").splitlines()
    filtered = filter_static_tests(raw_lines, static_lane)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _ = out_path.write_text("".join(f"{path}\n" for path in filtered), encoding="utf-8")
    print(
        f"test-cache filter-static: {len(filtered)} static test file(s) from {len(raw_lines)} input line(s)"
    )
    return 0


def _cmd_shard_targets(args: _CliArgs) -> int:
    if args.out is None:
        raise SystemExit("--out is required")
    if args.groups < 1 or args.group < 1 or args.group > args.groups:
        raise SystemExit(f"invalid group {args.group} for groups={args.groups}")
    repo_root = Path(args.repo).resolve()
    static_lane = frozenset(lanes_at(repo_root).static)
    plan_obj = _load_json_object(Path(args.plan))
    if plan_obj is None:
        raise SystemExit(f"{args.plan}: expected JSON object")
    run_list = _as_str_list(plan_obj.get("run"))
    skippable_count = len(_as_str_list(plan_obj.get("skippable")))
    targets = filter_static_tests(run_list, static_lane)
    durations = _load_durations(Path(args.durations) if args.durations else None)
    buckets = partition_targets(targets, durations, groups=args.groups)
    selected = buckets[args.group - 1]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _ = out_path.write_text("".join(f"{path}\n" for path in selected), encoding="utf-8")
    print(
        f"test-cache shard-targets: group {args.group}/{args.groups} -> "
        f"{len(selected)}/{len(targets)} static test file(s) (skippable={skippable_count})"
    )
    return 0


def _cmd_summarize(args: _CliArgs) -> int:
    plan_obj = _load_json_object(Path(args.plan))
    if plan_obj is None:
        raise SystemExit(f"{args.plan}: expected JSON object")
    report_obj: dict[str, object] | None = None
    if args.report:
        report_path = Path(args.report)
        if report_path.is_file():
            report_obj = _load_json_object(report_path)
    durations = _load_durations(Path(args.durations) if args.durations else None)
    summary = format_summary(
        plan=plan_obj,
        report=report_obj,
        durations=durations,
        skip_enabled=args.skip_enabled == "true",
        canary=args.canary == "true",
        recorded=args.recorded,
        label=args.label,
    )
    if args.out is not None:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as handle:
            _ = handle.write(summary)
    else:
        _ = sys.stdout.write(summary)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Static test result cache CI helper.")
    sub = parser.add_subparsers(dest="command", required=True)

    decide = sub.add_parser("decide", help="Decide whether static test cache skipping is active.")
    _ = decide.add_argument("--event", required=True)
    _ = decide.add_argument("--backend", required=True)
    _ = decide.add_argument("--mode", required=True)
    _ = decide.add_argument("--decision", default="")
    _ = decide.add_argument("--selected-count", default="0")
    _ = decide.add_argument("--pr-cache-hit", default="false")
    _ = decide.add_argument("--run-id", required=True)
    _ = decide.add_argument("--run-attempt", default="1")
    _ = decide.add_argument("--min-selected-count", type=int, default=_DEFAULT_MIN_SELECTED_COUNT)
    _ = decide.add_argument("--canary-percent", type=int, default=_DEFAULT_CANARY_PERCENT)
    _ = decide.add_argument("--github-output", default=None)

    filt = sub.add_parser("filter-static", help="Filter a test list down to the static lane.")
    _ = filt.add_argument("--in", dest="input_path", required=True)
    _ = filt.add_argument("--out", required=True)
    _ = filt.add_argument("--repo", default=".")

    shard = sub.add_parser("shard-targets", help="Partition plan.run static targets for one shard.")
    _ = shard.add_argument("--plan", required=True)
    _ = shard.add_argument("--durations", default=None)
    _ = shard.add_argument("--group", type=int, required=True)
    _ = shard.add_argument("--groups", type=int, required=True)
    _ = shard.add_argument("--out", required=True)
    _ = shard.add_argument("--repo", default=".")

    summ = sub.add_parser(
        "summarize", help="Render the Markdown step summary for test-cache-report."
    )
    _ = summ.add_argument("--plan", required=True)
    _ = summ.add_argument("--report", default=None)
    _ = summ.add_argument("--durations", default=None)
    _ = summ.add_argument("--skip-enabled", default="false")
    _ = summ.add_argument("--canary", default="false")
    _ = summ.add_argument("--recorded", type=int, default=0)
    _ = summ.add_argument("--label", default="")
    _ = summ.add_argument("--out", default=None)

    return parser


def main(argv: Sequence[str]) -> int:
    args = _build_parser().parse_args(list(argv), namespace=_CliArgs())
    if args.command == "decide":
        return _cmd_decide(args)
    if args.command == "filter-static":
        return _cmd_filter_static(args)
    if args.command == "shard-targets":
        return _cmd_shard_targets(args)
    if args.command == "summarize":
        return _cmd_summarize(args)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
