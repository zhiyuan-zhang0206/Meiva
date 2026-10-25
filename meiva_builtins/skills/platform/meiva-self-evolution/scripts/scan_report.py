# meiva:note
# Every scan prints one headline: `batch <total> runs · judgeable <n> (<share>) · unknown <n> (<reason counts>) · scored <n>`. A run is judgeable when its label is `ok`, `fumbled` or `failed`; the share is judgeable over total, and `n/a` when the window is empty; `scored` counts runs whose `rubric.scores(record)["overall"]` is not `None`, so it tracks what the rubric could actually judge. `alert_verdict` decides the exit code in one order: an empty window is a data-quality failure of the source (`empty-window`, unless the TEST- filter explains it), a judgeable share below the floor is `data-quality: low-coverage` naming the largest unknown reason, a `failed` or `fumbled` run is `regression`, and anything else exits 0. The render prints the low-coverage alert line but never lists an unknown run as worth mining. The coverage floor and the header's timestamp are values the caller passes in (`ScanView`), so the whole report is a function of its inputs.
# meiva:link meiva_builtins/skills/platform/meiva-self-evolution/docs/coverage-sentinel.meiva.okf.md
from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, TypeGuard

sys.path.insert(0, str(Path(__file__).resolve().parent))

import label
import rubric
from record import task_prompt_of

ORCHESTRATION_SKILLS = ("meiva-workflow", "meiva-dynamic-workflow", "meiva-goal")

# meiva:note
# The runner delivers only the output tail (last 2000 chars); the fixed report scaffold around the bad list costs ~450-600 chars, so the rich list is capped here and a longer one compacts to counter lines once the full report is persisted and pointed to. At the 2026-09-12 scale (48 bad runs) the compact view measures ~1500 chars; past ~55 runs the head lines leave the tail, while the summary + pointer always land.
BAD_LIST_MAX_CHARS = 1400


# meiva:note
# What one scan report is rendered for: the dataset it points to, the window's size in days, the collection counts (`None` when the caller has none), the clock reading for the header, and the judgeable share below which the scan alerts.
@dataclass(frozen=True)
class ScanView:
    path: Path
    days: int
    counts: Mapping[str, int] | None
    now: datetime
    coverage_floor: float


# meiva:note
# Human-readable signals explaining a non-ok label. Display-only; the label itself remains label.py's contract.
def _why(rec: dict[str, Any]) -> list[str]:
    why: list[str] = []
    if rec.get("corrections"):
        why.append(f"{len(rec['corrections'])} user correction(s)")
    if rec.get("peer_feedback"):
        why.append(f"{len(rec['peer_feedback'])} corrective peer message(s)")
    if rec.get("followup_prompts"):
        why.append(f"{len(rec['followup_prompts'])} follow-up re-prompt(s)")
    if rec.get("exec_failed"):
        why.append(f"{rec['exec_failed']} failed exec(s)")
    if rec.get("compactions"):
        why.append(f"{rec['compactions']} compaction(s)")
    if rec.get("last_exec_failed"):
        why.append("last exec failed")
    if (
        rec.get("terminated")
        and not rec.get("final_output", "").strip()
        and rec.get("turns", 0) > 0
    ):
        why.append("terminated without output")
    return why or ["no explicit signal"]


# meiva:note
# One bad run as counter tokens, the compact fallback for a bad list over BAD_LIST_MAX_CHARS (task text stays in the persisted full report). Tokens mirror _why(): rep=re-prompts, c=corrections, pf=peer feedback, ef=exec failures, last=last exec failed, cp=compactions, term=terminated without output.
def _compact_bad_line(rec: dict[str, Any]) -> str:
    parts: list[str] = []
    if rec.get("followup_prompts"):
        parts.append(f"rep{len(rec['followup_prompts'])}")
    if rec.get("corrections"):
        parts.append(f"c{len(rec['corrections'])}")
    if rec.get("peer_feedback"):
        parts.append(f"pf{len(rec['peer_feedback'])}")
    if rec.get("exec_failed"):
        parts.append(f"ef{rec['exec_failed']}")
    if rec.get("last_exec_failed"):
        parts.append("last")
    if rec.get("compactions"):
        parts.append(f"cp{rec['compactions']}")
    if (
        rec.get("terminated")
        and not rec.get("final_output", "").strip()
        and rec.get("turns", 0) > 0
    ):
        parts.append("term")
    return f"  #{rec['agent_id']} {rec['label']} " + (" ".join(parts) if parts else "-")


# meiva:note
# True when the window is empty only because the TEST- filter removed every run the source produced: a quiet day, not an outage.
def _is_test_only_window(records: list[dict[str, Any]], counts: Mapping[str, int] | None) -> bool:
    if records:
        return False
    return bool(counts and counts["seen"] > 0 and counts["excluded_test"] == counts["seen"])


class Alert(NamedTuple):
    code: int
    reason: str | None


def _is_str_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def alert_verdict(
    records: Sequence[Mapping[str, object]], counts: Mapping[str, int] | None, coverage_floor: float
) -> Alert:
    rows = [r if _is_str_dict(r) else dict(r) for r in records]
    return _verdict(rows, counts, coverage_floor)


def _verdict(
    records: list[dict[str, Any]], counts: Mapping[str, int] | None, coverage_floor: float
) -> Alert:
    if not records:
        if _is_test_only_window(records, counts):
            return Alert(0, None)
        return Alert(2, "empty-window")
    totals = _Totals.of(records)
    coverage = _coverage(totals)
    if coverage is not None and coverage < coverage_floor:
        return Alert(2, "data-quality: low-coverage")
    if any(r["label"] in label.REGRESSION_LABELS for r in records):
        return Alert(2, "regression")
    return Alert(0, None)


# meiva:note
# The headline counters of one scan window.
class _Totals(NamedTuple):
    by_label: Counter[str]
    corrections: int
    peer: int
    execfail: int
    unknown_reasons: Counter[str]
    total: int
    scored: int

    @classmethod
    def of(cls, records: list[dict[str, Any]]) -> _Totals:
        return cls(
            Counter(r["label"] for r in records),
            sum(len(r["corrections"]) for r in records),
            sum(len(r["peer_feedback"]) for r in records),
            sum(1 for r in records if r["exec_failed"]),
            Counter(
                r.get("unknown_reason") or "unspecified" for r in records if r["label"] == "unknown"
            ),
            len(records),
            sum(1 for r in records if rubric.scores(r).get("overall") is not None),
        )


def _coverage(totals: _Totals) -> float | None:
    if not totals.total:
        return None
    return (totals.total - totals.by_label["unknown"]) / totals.total


def _coverage_line(totals: _Totals) -> str:
    coverage = _coverage(totals)
    share = "n/a" if coverage is None else f"{coverage:.1%}"
    unknown = totals.by_label["unknown"]
    reasons = ", ".join(f"{name} {count}" for name, count in sorted(totals.unknown_reasons.items()))
    return (
        f"batch {totals.total} runs · judgeable {totals.total - unknown} ({share})"
        f" · unknown {unknown}{f' ({reasons})' if reasons else ''}"
        f" · scored {totals.scored}"
    )


def _coverage_alert_line(totals: _Totals, floor: float) -> str | None:
    coverage = _coverage(totals)
    if coverage is None or coverage >= floor:
        return None
    if totals.unknown_reasons:
        reason, count = totals.unknown_reasons.most_common(1)[0]
    else:
        reason, count = "unspecified", 0
    return (
        f"ALERT — data-quality: low-coverage: judgeable {coverage:.1%} < floor {floor:.0%};"
        f" largest unknown gap {reason} ({count} run(s))"
    )


def _label_counts(totals: _Totals) -> str:
    unknown = totals.by_label["unknown"]
    detail = ""
    if unknown:
        detail = (
            " ("
            + ", ".join(
                f"{reason} {count}" for reason, count in sorted(totals.unknown_reasons.items())
            )
            + ")"
        )
    return (
        f"ok {totals.by_label['ok']} / fumbled {totals.by_label['fumbled']} / "
        f"failed {totals.by_label['failed']} / unknown {unknown}{detail}"
    )


def _builtin_help_line(records: list[dict[str, Any]]) -> str:
    calls = sum(r.get("builtin_help_calls", 0) for r in records)
    agents = sum(bool(r.get("builtin_help_calls", 0)) for r in records)
    on_meiva = sum(r.get("builtin_help_on_meiva", 0) for r in records)
    return f"builtin help(): {calls} calls across {agents} agents ({on_meiva} on meiva.* targets)"


def _subprocess_line(records: list[dict[str, Any]]) -> str:
    calls = sum(r.get("subprocess_calls", 0) for r in records)
    line = f"subprocess calls: {calls}"
    if any("tools_called" in r for r in records):
        shell_run_calls = sum(
            r["tools_called"].get("meiva.shell.run", 0) for r in records if "tools_called" in r
        )
        line += f" (shell.run {shell_run_calls})"
    return line


def _header_lines(records: list[dict[str, Any]], view: ScanView, totals: _Totals) -> list[str]:
    skill_counts = Counter(
        skill for r in records if "skills_touched" in r for skill in r["skills_touched"]
    )
    top_skills = (
        ", ".join(f"{skill} {count}" for skill, count in skill_counts.most_common(5)) or "none"
    )
    orchestration_counts = ", ".join(
        f"{skill} {skill_counts[skill]}" for skill in ORCHESTRATION_SKILLS
    )
    return [
        f"self-evolution daily scan — {view.now.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"window: {view.days} day(s) | runs: {len(records)} ({_label_counts(totals)})",
        _coverage_line(totals),
        f" | corrections {totals.corrections} | peer feedback {totals.peer} | exec-fail runs {totals.execfail}",
        _builtin_help_line(records),
        _subprocess_line(records),
        f"dataset: {view.path}",
        "skills loaded:",
        f"  top 5: {top_skills}",
        f"  orchestration: {orchestration_counts}",
    ]


def _no_runs_line(records: list[dict[str, Any]], counts: Mapping[str, int] | None) -> str:
    if counts is not None and _is_test_only_window(records, counts):
        return (
            "0 production runs — all "
            f"{counts['excluded_test']} window agent(s) were TEST- spawns "
            "(excluded by design); nothing to act on"
        )
    return "ALERT — 0 runs collected: data source outage or collector failure"


def _bad_run_lines(
    records: list[dict[str, Any]], *, report_path: Path | None, compact_bad: bool
) -> list[str]:
    bad = [r for r in records if r["label"] in label.REGRESSION_LABELS]
    if not bad:
        return []
    sorted_bad = sorted(bad, key=lambda r: r["agent_id"])
    rich_lines: list[str] = []
    for rec in sorted_bad:
        task = task_prompt_of(rec).strip().replace("\n", " ")
        if len(task) > 120:
            task = task[:117] + "..."
        line = f"  #{rec['agent_id']} {rec['label']} — {', '.join(_why(rec))}"
        if task:
            line += f" | task: {task}"
        rich_lines.append(line)
    head = f"ALERT — {len(bad)} run(s) worth mining:"
    if (
        compact_bad
        and report_path is not None
        and sum(len(line) for line in rich_lines) > BAD_LIST_MAX_CHARS
    ):
        return [
            f"{head} (compact; details in report)",
            *(_compact_bad_line(rec) for rec in sorted_bad),
        ]
    return [head, *rich_lines]


# meiva:note
# Render the scan report. With `report_path` set the output closes with a summary line and a pointer to the persisted full report. `compact_bad` may replace an oversized bad list (> BAD_LIST_MAX_CHARS chars) with one counter line per run, but only when `report_path` is set: compaction needs a stored report, whose closing pointer keeps the dropped task text one hop away and survives tail truncation, and without one the rich lines are the only copy of the details.
def render(
    records: Sequence[Mapping[str, object]],
    view: ScanView,
    *,
    report_path: Path | None = None,
    compact_bad: bool = False,
) -> str:
    rows = [r if _is_str_dict(r) else dict(r) for r in records]
    return _render(rows, view, report_path=report_path, compact_bad=compact_bad)


def _render(
    records: list[dict[str, Any]], view: ScanView, *, report_path: Path | None, compact_bad: bool
) -> str:
    totals = _Totals.of(records)
    lines = _header_lines(records, view, totals)
    if not records:
        lines.append(_no_runs_line(records, view.counts))
    else:
        coverage_alert = _coverage_alert_line(totals, view.coverage_floor)
        if coverage_alert is not None:
            lines.append(coverage_alert)
    lines.extend(_bad_run_lines(records, report_path=report_path, compact_bad=compact_bad))
    if report_path is not None:
        lines.append(
            f"summary: {len(records)} runs ({_label_counts(totals)})"
            f" | corrections {totals.corrections} | peer {totals.peer} "
            f"| exec-fail runs {totals.execfail}"
        )
        lines.append(f"full report: {report_path}")
    return "\n".join(lines)
