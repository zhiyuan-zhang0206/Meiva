#!/usr/bin/env python3
"""Aggregate self_evolution pipeline outputs into the JSON consumed by `render_report.py`.

Reads:
  - A changes manifest (hand-written or tool-generated): [{skill, summary, pr, pr_url}, ...]
  - evaluate.py `gather` output (one JSON file per skill)

Produces:
  - A single JSON file ready for `render_report.py` → `meiva.ui.serve()`

Usage:
    .venv/bin/python skills/meiva-self-evolution/scripts/aggregate.py \
        --changes changes.json \
        --eval-dir ~/.meiva/self_evolution/eval/ \
        --failure-md failure-clusters.md \
        --week 2025-06-30 \
        --out report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import TypeAdapter, ValidationError

from base.host.boundary.options import options
from base.host.boundary.untyped import object_of

# `render_report` is a sibling in this skill's own scripts/ dir, not an
# importable package (PYTHONSAFEPATH=1 keeps a script's own directory off
# sys.path) — restore it for the sibling import used below. Structure Rule 6
# recognizes this exact __file__-derived, within-skill shape as the endorsed
# guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate import ScoreMean
from render_report import (
    ClusterRun,
    EvalResults,
    FailureCluster,
    ScoreDimension,
    ScoreSkill,
    build_report_data,
    read_changes,
)


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as f:
        return object_of(json.load, f)


# meiva:note
# The text of one captured regex group. `re.Match.group` answers `str | Any` (a group that did not
# participate is `None`), so a `str` built from it is only checked here.
def _captured(match: re.Match[str], index: int) -> str:
    value = match.group(index)
    if not isinstance(value, str):
        raise TypeError(f"group {index} of {match.re.pattern!r} captured no text")
    return value


def parse_failure_md(text: str) -> list[FailureCluster]:
    """Parse mine.py's markdown output back into structured clusters.

    The markdown format is:
        ## skill_name — N bad runs (F failed, U fumbled)
        - run #agent_id [label] "task prompt"  (signals)
    """
    clusters: list[FailureCluster] = []
    current: FailureCluster | None = None

    for line in text.splitlines():
        h2 = re.match(r"^## (.+?) — (\d+) bad runs \((\d+) failed, (\d+) fumbled\)$", line)
        if h2:
            if current:
                clusters.append(current)
            current = FailureCluster(
                skill=_captured(h2, 1),
                count=int(_captured(h2, 2)),
                failed=int(_captured(h2, 3)),
                fumbled=int(_captured(h2, 4)),
                top_signals=[],
                runs=[],
            )
            continue

        run_match = re.match(r"^- run #(\d+) \[(\w+)\] \"(.+?)\"\s+\((.+?)\)$", line)
        if run_match and current is not None:
            agent_id = int(_captured(run_match, 1))
            label = _captured(run_match, 2)
            task = _captured(run_match, 3)
            signals = [s.strip() for s in _captured(run_match, 4).split(",") if s.strip()]
            current["runs"].append(
                ClusterRun(agent_id=agent_id, label=label, task_snippet=task[:140])
            )
            for sig in signals:
                if sig not in current["top_signals"] and sig != "(no signal)":
                    current["top_signals"].append(sig)

    if current:
        clusters.append(current)
    return clusters


# meiva:note
# The three dimensions a mean carries, in the order the template draws them.
_SCORE_DIMENSIONS: tuple[Literal["completion", "efficiency", "overall"], ...] = (
    "completion",
    "efficiency",
    "overall",
)


def compute_score_dimensions(before: ScoreMean, after: ScoreMean) -> list[ScoreDimension]:
    """Convert two {completion, efficiency, overall} dicts into the dimension list
    the report template expects."""
    dims: list[ScoreDimension] = []
    for key in _SCORE_DIMENSIONS:
        b = before[key]
        a = after[key]
        delta = round(a - b, 3)
        dims.append(
            ScoreDimension(
                label=key,
                before=b,
                after=a,
                before_pct=round(b * 100),
                after_pct=round(a * 100),
                delta=delta,
                delta_pct=round(delta * 100),
                delta_str=f"{delta:+.3f}",
                delta_class="delta-up"
                if delta > 0
                else ("delta-down" if delta < 0 else "delta-flat"),
            )
        )
    return dims


# meiva:note
# The fields of one `evaluate.gather` report this aggregator reads. `n` is missing from reports
# written before it was recorded, and a mean may carry `None` for a dimension that scored nothing
# (an abstained stamp), so both are optional here: `measured_mean` is what decides whether there is
# a mean to compare a later stamp against.
class EvalSummary(TypedDict, total=False):
    skill: str
    n: int
    mean: dict[str, float | None] | None


_EVAL_SUMMARY = TypeAdapter(EvalSummary)


# meiva:note
# The mean a `gather` report carries, or nothing when it scored no run: a report whose `n` is zero
# has no mean to compare against, and a mean with a `None` dimension is an abstention, not a score.
def measured_mean(summary: EvalSummary) -> ScoreMean | None:
    if summary.get("n", 0) <= 0:
        return None
    mean = summary.get("mean")
    if mean is None:
        return None
    completion = mean.get("completion")
    efficiency = mean.get("efficiency")
    overall = mean.get("overall")
    if completion is None or efficiency is None or overall is None:
        return None
    return ScoreMean(completion=completion, efficiency=efficiency, overall=overall)


# meiva:note
# The inputs this aggregator reads and the report it writes: the changes manifest, the eval reports
# directory, the failure digest, the week label, the dataset's run count and the output path.
@dataclass(frozen=True)
class AggregateOptions:
    changes: str
    eval_dir: str | None
    failure_md: str | None
    week: str
    total_runs: int
    out: str


def main(argv: Sequence[str], now: datetime) -> None:
    p = argparse.ArgumentParser(description="Aggregate self_evolution outputs into a report JSON.")
    p.add_argument("--changes", required=True, help="JSON list of {skill, summary, pr, pr_url}")
    p.add_argument("--eval-dir", help="directory containing evaluate.py gather outputs")
    p.add_argument("--failure-md", help="mine.py markdown output file")
    p.add_argument("--week", required=True, help="ISO week label e.g. 2025-06-30")
    p.add_argument("--total-runs", type=int, required=True, help="total runs in dataset")
    p.add_argument("--out", default="report.json", help="output path")
    opts = options(p.parse_args(list(argv)), AggregateOptions)

    # Changes
    try:
        changes = read_changes(load_json(Path(opts.changes)))
    except ValidationError as exc:
        print(f"ERROR: --changes must be a JSON list of changes: {exc}", file=sys.stderr)
        sys.exit(1)

    # Eval scores — gather outputs are per-skill JSON files
    score_skills: list[ScoreSkill] = []
    if opts.eval_dir:
        eval_dir = Path(opts.eval_dir)
        previous: dict[str, ScoreMean] = {}
        for f in sorted(eval_dir.glob("*.json")):
            try:
                summary = _EVAL_SUMMARY.validate_python(load_json(f), strict=True)
            except ValidationError as exc:
                print(f"warning: skipping {f}: not an eval report: {exc}", file=sys.stderr)
                continue
            skill = summary.get("skill", f.stem.rsplit("-", 1)[0])
            mean = measured_mean(summary)
            before = previous.get(skill)
            dims = (
                compute_score_dimensions(before, mean)
                if before is not None and mean is not None
                else []
            )
            if mean is not None:
                previous[skill] = mean
            score_skills.append(
                ScoreSkill(
                    skill=skill,
                    dimensions=dims,
                    n=summary.get("n", 0),
                    unavailable=mean is None,
                )
            )

    # Failure clusters from mine.py output
    failure_clusters: list[FailureCluster] = []
    if opts.failure_md:
        md_text = Path(opts.failure_md).read_text(encoding="utf-8")
        failure_clusters = parse_failure_md(md_text)

    eval_results: EvalResults | None = None

    data = build_report_data(
        week=opts.week,
        changes=changes,
        score_skills=score_skills,
        failure_clusters=failure_clusters,
        eval_results=eval_results,
        total_runs=opts.total_runs,
        now=now,
    )

    out_path = Path(opts.out)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path} ({len(json.dumps(data))} bytes)")


if __name__ == "__main__":
    main(sys.argv[1:], datetime.now(UTC))
