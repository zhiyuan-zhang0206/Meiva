#!/usr/bin/env python3
"""Render a self_evolution report JSON into a self-contained HTML page.

Reads a JSON report file and renders it against `report_template.html` (in
this skill's `assets/`, a non-code sibling of the `scripts/` this module lives
in) into a standalone HTML page. The template uses
`<!-- SECTION_NAME -->...<!-- END_SECTION_NAME -->` markers; each section is
replaced with the corresponding rendered content.

Usage:

    .venv/bin/python skills/meiva-self-evolution/scripts/render_report.py <report.json> [--output report.html]
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
from typing import NotRequired, TypedDict

from pydantic import TypeAdapter

from base.host.boundary.options import options
from base.host.boundary.untyped import object_of


# meiva:note
# One score dimension of one skill as the template draws it: the two readings, their percentages
# and the delta this round.
class ScoreDimension(TypedDict):
    label: str
    before: float
    after: float
    before_pct: int
    after_pct: int
    delta: float
    delta_pct: int
    delta_str: str
    delta_class: str


# meiva:note
# One skill's dimension list, or the reason it has none (`unavailable`: too few scorable runs).
class ScoreSkill(TypedDict):
    skill: str
    dimensions: list[ScoreDimension]
    n: NotRequired[int]
    unavailable: NotRequired[bool]


# meiva:note
# One entry of the hand-written changes manifest: the skill, what changed and where to read it.
class ChangeEntry(TypedDict):
    skill: str
    summary: str
    pr: NotRequired[int | str]
    pr_url: NotRequired[str]


# meiva:note
# One bad run named in a failure cluster, as `mine.py`'s digest carries it.
class ClusterRun(TypedDict):
    agent_id: int
    label: str
    task_snippet: NotRequired[str]


# meiva:note
# One failure cluster: the skill, its counts and the runs behind them.
class FailureCluster(TypedDict):
    skill: str
    count: int
    failed: int
    fumbled: int
    top_signals: list[str]
    runs: list[ClusterRun]


# meiva:note
# One skill's replay results in the evaluation section.
class _EvalSkillRowRequired(TypedDict):
    skill: str


class EvalSkillRow(_EvalSkillRowRequired, total=False):
    replayed: int
    improved: int
    regressed: int
    verdict: str


# meiva:note
# The evaluation section: how many tasks were replayed and how they moved. A producer that only
# knows some of the counts leaves the rest out.
class EvalResults(TypedDict, total=False):
    replayed: int
    old_ok: int
    new_ok: int
    improved: int
    regressed: int
    per_skill: list[EvalSkillRow]


# meiva:note
# The whole report the template renders: the header scalars the placeholders name and the four
# sections below them.
class ReportData(TypedDict):
    week: str
    generated_at: str
    skill_count: int
    total_runs: int
    changes_count: int
    overall_delta: str
    tasks_replayed: int
    improved: int
    regressed: int
    cluster_count: int
    cluster_skills: int
    changes: list[ChangeEntry]
    score_skills: list[ScoreSkill]
    failure_clusters: list[FailureCluster]
    eval_results: EvalResults | None


_REPORT = TypeAdapter(ReportData)
_CHANGES = TypeAdapter(list[ChangeEntry])


# meiva:note
# One report JSON's data, checked field by field against the shape above. A key the shape does not
# name is dropped here; a required one that is missing is a broken report, not a default.
def read_report_data(value: object) -> ReportData:
    return _REPORT.validate_python(value, strict=True)


# meiva:note
# The changes manifest a reader hands in: a list of entries, each checked against `ChangeEntry`.
def read_changes(value: object) -> list[ChangeEntry]:
    return _CHANGES.validate_python(value, strict=True)


# meiva:note
# The two paths this entry reads: the report to render and where to write the page.
@dataclass(frozen=True)
class RenderOptions:
    report_json: str
    output: str | None


# ── Template ────────────────────────────────────────────────────────


def _load_template() -> str:
    tpl_path = Path(__file__).resolve().parent.parent / "assets" / "report_template.html"
    return tpl_path.read_text(encoding="utf-8")


# ── Section renderers ───────────────────────────────────────────────


def _na_card(skill: str, reason: str) -> str:
    return f"""<div class="score-card">
    <div class="score-skill-name">{skill}</div>
    <div class="score-row"><span class="score-dim">n/a — {reason}</span></div>
  </div>"""


def _render_score_skills(data: ReportData) -> str:
    skills = data.get("score_skills")
    if not skills:
        return """<div class="empty">No score comparison data for this round.</div>"""

    cards: list[str] = []
    for s in skills:
        if s.get("unavailable"):
            cards.append(_na_card(s["skill"], f"no scorable runs ({s.get('n', 0)} replayed)"))
            continue
        dims_html: list[str] = []
        for d in s.get("dimensions", []):
            dims_html.append(
                f"""<div class="score-row">
      <span class="score-dim">{d["label"]}</span>
      <span class="score-before">{d["before"]}</span>
      <span class="score-arrow">→</span>
      <span class="score-after">{d["after"]}</span>
      <div class="score-bar-wrap">
        <div class="score-bar-before" style="width:{min(d["before_pct"], 95)}%"></div>
        <div class="score-bar-after" style="width:{abs(d["delta_pct"])}%"></div>
      </div>
      <span class="score-delta {d["delta_class"]}">{d["delta_str"]}</span>
    </div>"""
            )
        if not dims_html:
            cards.append(_na_card(s["skill"], "no earlier stamp to compare"))
            continue
        cards.append(
            f"""<div class="score-card">
    <div class="score-skill-name">{s["skill"]}</div>
    {"".join(dims_html)}
  </div>"""
        )

    return f"""<div class="score-grid">
  {"".join(cards)}
</div>"""


def _render_changes_table(data: ReportData) -> str:
    changes = data.get("changes")
    if not changes:
        return """<div class="empty">No changes recorded this round.</div>"""

    rows: list[str] = []
    for c in changes:
        pr = c.get("pr")
        pr_url = c.get("pr_url")
        pr_cell = (
            f'<a href="{pr_url}" style="color:#58a6ff">#{pr}</a>'
            if pr and pr_url
            else (f"#{pr}" if pr else "&mdash;")
        )
        rows.append(
            f"""<tr>
      <td style="font-weight:500;color:#f0f6fc">{c["skill"]}</td>
      <td>{c["summary"]}</td>
      <td style="font-family:monospace;font-size:0.8rem">{pr_cell}</td>
    </tr>"""
        )

    return f"""<table>
  <thead><tr><th>Skill</th><th>Summary</th><th>PR</th></tr></thead>
  <tbody>
  {"".join(rows)}
  </tbody>
</table>"""


def _render_failure_clusters(data: ReportData) -> str:
    clusters = data.get("failure_clusters")
    if not clusters:
        return """<div class="empty">No failures this week — all runs completed cleanly.</div>"""

    parts: list[str] = []
    for cl in clusters:
        # Badges
        badges: list[str] = []
        if cl.get("failed", 0) > 0:
            badges.append(f'<span class="cluster-badge badge-failed">{cl["failed"]} failed</span>')
        if cl.get("fumbled", 0) > 0:
            badges.append(
                f'<span class="cluster-badge badge-fumbled">{cl["fumbled"]} fumbled</span>'
            )

        # Signals
        signals_html = ""
        if cl.get("top_signals"):
            tags = "".join(f'<span class="signal-tag">{s}</span>' for s in cl["top_signals"])
            signals_html = f'<div class="cluster-signals">{tags}</div>'

        # Runs (show up to 8, note overflow)
        runs_html = ""
        if cl.get("runs"):
            shown = cl["runs"][:8]
            run_items: list[str] = []
            for r in shown:
                label_cls = "label-failed" if r.get("label") == "failed" else "label-fumbled"
                run_items.append(
                    f"""<div class="run-item">
      #{r["agent_id"]}
      <span class="run-label {label_cls}">[{r["label"]}]</span>
      &mdash; {r.get("task_snippet", "")}
    </div>"""
                )
            overflow = ""
            if len(cl["runs"]) > 8:
                overflow = (
                    f'<div class="run-item" style="color:#484f58">'
                    f"... and {len(cl['runs']) - 8} more</div>"
                )
            runs_html = f"""<div class="run-list">
  {"".join(run_items)}
  {overflow}
  </div>"""

        parts.append(
            f"""<div class="cluster">
  <div class="cluster-header">
    <span class="cluster-skill">{cl["skill"]}</span>
    <span class="cluster-count">
      {cl["count"]} bad runs
      {"".join(badges)}
    </span>
  </div>
  {signals_html}
  {runs_html}
</div>"""
        )

    return "\n".join(parts)


def _render_eval_results(data: ReportData) -> str:
    er = data.get("eval_results")
    if not er:
        return """<div class="empty">No eval data for this round.</div>"""

    def _verdict_color(v: str) -> str:
        if v in ("better", "improved"):
            return "#3fb950"
        if v in ("worse", "regressed"):
            return "#f85149"
        return "#8b949e"

    summary = f"""<div class="eval-summary">
  <div class="eval-stat">
    <div class="eval-stat-value">{er.get("replayed", 0)}</div>
    <div class="eval-stat-label">tasks replayed</div>
  </div>
  <div class="eval-stat">
    <div class="eval-stat-value" style="color:#f85149">{er.get("old_ok", 0)}</div>
    <div class="eval-stat-label">before (ok)</div>
  </div>
  <div class="eval-stat">
    <div class="eval-stat-value" style="color:#3fb950">{er.get("new_ok", 0)}</div>
    <div class="eval-stat-label">after (ok)</div>
  </div>
  <div class="eval-stat">
    <div class="eval-stat-value" style="color:#3fb950">{er.get("improved", 0)}</div>
    <div class="eval-stat-label">improved</div>
  </div>
  <div class="eval-stat">
    <div class="eval-stat-value" style="color:#f85149">{er.get("regressed", 0)}</div>
    <div class="eval-stat-label">regressed</div>
  </div>
</div>"""

    per_skill = er.get("per_skill")
    per_skill_html = ""
    if per_skill:
        rows: list[str] = []
        for es in per_skill:
            v = es.get("verdict", "")
            rows.append(
                f"""<tr>
      <td style="font-weight:500;color:#f0f6fc">{es["skill"]}</td>
      <td>{es.get("replayed", 0)}</td>
      <td style="color:#3fb950">{es.get("improved", 0)}</td>
      <td style="color:#f85149">{es.get("regressed", 0)}</td>
      <td><span style="color:{_verdict_color(v)}">{v}</span></td>
    </tr>"""
            )
        per_skill_html = f"""<table>
  <thead><tr><th>Skill</th><th>Replayed</th><th>Improved</th><th>Regressed</th><th>Verdict</th></tr></thead>
  <tbody>
  {"".join(rows)}
  </tbody>
</table>"""

    return summary + per_skill_html


# ── Render ──────────────────────────────────────────────────────────


# meiva:note
# The header scalars the template's `{{ name }}` placeholders stand for, as the strings they render
# as. Listed one by one rather than read off the report: a placeholder that names anything else is
# a template bug the empty substitution reports, not a value to guess at.
def _placeholders(data: ReportData) -> dict[str, str]:
    return {
        "week": data["week"],
        "generated_at": data["generated_at"],
        "skill_count": str(data["skill_count"]),
        "total_runs": str(data["total_runs"]),
        "changes_count": str(data["changes_count"]),
        "overall_delta": data["overall_delta"],
        "tasks_replayed": str(data["tasks_replayed"]),
        "improved": str(data["improved"]),
        "regressed": str(data["regressed"]),
        "cluster_count": str(data["cluster_count"]),
        "cluster_skills": str(data["cluster_skills"]),
    }


def render_report(data: ReportData) -> str:
    """Render the full HTML report from assembled data."""
    template = _load_template()
    placeholders = _placeholders(data)

    # Substitute simple {{ var }} placeholders
    # meiva:note
    # `re.Match.group` answers `str | Any` (a group that did not participate is `None`); the
    # placeholder name is checked before it selects a value.
    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if not isinstance(key, str):
            return ""
        return placeholders.get(key.strip(), "")

    html = re.sub(r"\{\{\s*(.+?)\s*\}\}", _sub, template)

    # Replace sections
    sections = {
        "SCORE_SKILLS": _render_score_skills(data),
        "CHANGES_TABLE": _render_changes_table(data),
        "FAILURE_CLUSTERS": _render_failure_clusters(data),
        "EVAL_RESULTS": _render_eval_results(data),
    }

    for name, content in sections.items():
        html = re.sub(
            rf"<!-- {name} -->.*?<!-- END_{name} -->",
            f"<!-- {name} -->\n{content}\n<!-- END_{name} -->",
            html,
            flags=re.DOTALL,
        )

    return html


# ── Data builder (convenience) ──────────────────────────────────────


def build_report_data(
    *,
    week: str,
    changes: list[ChangeEntry],
    score_skills: list[ScoreSkill],
    failure_clusters: list[FailureCluster],
    now: datetime,
    eval_results: EvalResults | None = None,
    total_runs: int = 0,
) -> ReportData:
    """Assemble the data context consumed by `render_report`.

    See the JSON schema documented in the template's companion README or the
    sample at `skills/meiva-self-evolution/references/sample_report.json`.
    """
    deltas = [
        dimension["delta"]
        for skill in score_skills
        for dimension in skill.get("dimensions", [])
        if dimension["label"] == "overall"
    ]
    overall_delta = f"{round(sum(deltas) / len(deltas), 3):+.3f}" if deltas else "n/a"

    tasks_replayed = eval_results.get("replayed", 0) if eval_results else 0
    improved = eval_results.get("improved", 0) if eval_results else 0
    regressed = eval_results.get("regressed", 0) if eval_results else 0

    return ReportData(
        week=week,
        generated_at=now.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        skill_count=len(score_skills),
        total_runs=total_runs,
        changes_count=len(changes),
        overall_delta=overall_delta,
        tasks_replayed=tasks_replayed,
        improved=improved,
        regressed=regressed,
        cluster_count=len(failure_clusters),
        cluster_skills=len({entry["skill"] for entry in failure_clusters}),
        changes=changes,
        score_skills=score_skills,
        failure_clusters=failure_clusters,
        eval_results=eval_results,
    )


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: Sequence[str]) -> None:
    p = argparse.ArgumentParser(description="Render a self_evolution report JSON to HTML.")
    p.add_argument("report_json", help="path to the aggregated report JSON")
    p.add_argument(
        "--output",
        "-o",
        default=None,
        help="output HTML path (default: <report>.html)",
    )
    opts = options(p.parse_args(list(argv)), RenderOptions)
    in_path = Path(opts.report_json)
    data = read_report_data(object_of(json.loads, in_path.read_text(encoding="utf-8")))
    html = render_report(data)

    out_path = Path(opts.output) if opts.output else in_path.with_suffix(".html")
    out_path.write_text(html, encoding="utf-8")
    print(f"Rendered {out_path} ({len(html)} bytes)")


if __name__ == "__main__":
    main(sys.argv[1:])
