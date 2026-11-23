# meiva:link docs/conventions/engineering/test-selection.md

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict, cast

from meizel.api import lanes_at

from ..shard_counts import load_durations

_STATIC_SHARD_WORKER_SECONDS = 4 * 120
_MAX_STATIC_SHARDS = 8
_COLD_FILE_SECONDS = 10.0


class _CliArgs(argparse.Namespace):
    command: str = ""
    input_path: Path = Path()
    out: Path = Path()
    repo: Path = Path()
    plan: Path = Path()
    report: Path | None = None
    recorded: int = 0
    label: str = ""
    github_output: Path = Path()


class StaticMatrixRow(TypedDict):
    group: int
    digest: str
    min_tests: str


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


def static_shards(
    selected: Sequence[str],
    static_lane: frozenset[str],
    node_seconds: Mapping[str, float],
) -> list[list[str]]:
    targets = filter_static_tests(selected, static_lane)
    if not targets:
        return [[]]
    file_seconds: dict[str, float] = {}
    for node_id, seconds in node_seconds.items():
        path, separator, _ = node_id.partition("::")
        if separator and path in static_lane and math.isfinite(seconds) and seconds >= 0:
            file_seconds[path] = file_seconds.get(path, 0.0) + seconds
    measured = sorted(file_seconds.values())
    fallback = (
        max(1.0, measured[math.ceil(len(measured) * 0.9) - 1]) if measured else _COLD_FILE_SECONDS
    )
    weights = {path: file_seconds.get(path, fallback) for path in targets}
    groups = min(
        _MAX_STATIC_SHARDS,
        len(targets),
        max(1, math.ceil(sum(weights.values()) / _STATIC_SHARD_WORKER_SECONDS)),
    )
    shards = partition_targets(targets, weights, groups=groups)
    if not all(shards) or sorted(path for shard in shards for path in shard) != sorted(targets):
        raise ValueError("Static shard plan must cover each selected static file exactly once")
    return shards


def publish_static_plan(directory: Path, shards: Sequence[Sequence[str]]) -> list[StaticMatrixRow]:
    rows: list[StaticMatrixRow] = []
    for group, paths in enumerate(shards, start=1):
        payload = "".join(f"{path}\n" for path in paths).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        (directory / f"static-{group}.txt").write_bytes(payload)
        rows.append({"group": group, "digest": digest, "min_tests": "1" if paths else "0"})
        print(f"static selection: group={group} files={len(paths)} sha256={digest}")
    all_paths = sorted(path for shard in shards for path in shard)
    digest = hashlib.sha256("".join(f"{path}\n" for path in all_paths).encode("utf-8")).hexdigest()
    print(f"static selection: groups={len(shards)} files={len(all_paths)} sha256={digest}")
    return rows


def format_summary(
    *,
    plan: Mapping[str, object],
    report: Mapping[str, object],
    recorded: int,
    label: str,
) -> str:
    would_skip = report.get("would_skip", len(_as_str_list(plan.get("skippable"))))
    would_skip_passed = report.get("would_skip_passed", 0)
    failed_list = _as_str_list(report.get("would_skip_failed"))
    not_run_count = len(_as_str_list(report.get("would_skip_not_run")))
    raw_saved = report.get("saved_seconds", 0.0)
    saved_val = float(raw_saved) if isinstance(raw_saved, (int, float)) else 0.0
    uncacheable = report.get("uncacheable", _as_mapping_len(plan.get("uncacheable")))
    unbounded = report.get("unbounded", _as_mapping_len(plan.get("unbounded")))
    lines = [
        f"### Test result cache ({label})",
        f"- would skip: {would_skip} ({would_skip_passed} passed when run)",
        f"- would skip but failed: {len(failed_list)} {failed_list}",
        f"- would skip, not run: {not_run_count}",
        f"- seconds represented by hits (by `.test_durations`): {saved_val:.0f}",
        f"- recorded: {recorded}",
        f"- uncacheable: {uncacheable}; keyed by the whole tree: {unbounded}",
    ]
    return "\n".join(lines) + "\n"


def _cmd_plan_static(args: _CliArgs) -> int:
    repo = args.repo.resolve()
    paths = args.input_path.read_text(encoding="utf-8").splitlines()
    timings_path = repo / ".test_durations"
    try:
        timings = load_durations(timings_path)
    except (OSError, ValueError, TypeError) as exc:
        print(f"::warning::Ignoring unusable static durations: {exc}")
        timings = {}
    shards = static_shards(paths, lanes_at(repo).static, timings)
    matrix = publish_static_plan(args.input_path.parent, shards)
    with args.github_output.open("a", encoding="utf-8") as handle:
        _ = handle.write(f"matrix={json.dumps({'include': matrix})}\n")
    return 0


def _cmd_summarize(args: _CliArgs) -> int:
    plan = _load_json_object(args.plan)
    if plan is None:
        raise SystemExit(f"{args.plan}: expected JSON object")
    report = _load_json_object(args.report) if args.report is not None else None
    summary = format_summary(
        plan=plan, report=report or {}, recorded=args.recorded, label=args.label
    )
    with args.out.open("a", encoding="utf-8") as handle:
        _ = handle.write(summary)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CI static test planning and cache reporting.")
    sub = parser.add_subparsers(dest="command", required=True)

    shard = sub.add_parser("plan-static", help="Balance selected static files across CI jobs.")
    _ = shard.add_argument("--in", dest="input_path", type=Path, required=True)
    _ = shard.add_argument("--repo", type=Path, default=Path())
    _ = shard.add_argument("--github-output", type=Path, required=True)

    summ = sub.add_parser(
        "summarize", help="Render the Markdown step summary for test-cache-report."
    )
    _ = summ.add_argument("--plan", type=Path, required=True)
    _ = summ.add_argument("--report", type=Path, default=None)
    _ = summ.add_argument("--recorded", type=int, default=0)
    _ = summ.add_argument("--label", default="shadow")
    _ = summ.add_argument("--out", type=Path, required=True)

    return parser


def main(argv: Sequence[str]) -> int:
    args = _build_parser().parse_args(list(argv), namespace=_CliArgs())
    if args.command == "plan-static":
        return _cmd_plan_static(args)
    if args.command == "summarize":
        return _cmd_summarize(args)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
