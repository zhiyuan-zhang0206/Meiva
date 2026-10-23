# meiva:link schedules/docs/adversarial-eval.meiva.okf.md

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

from schedules.adversarial_eval_model import (
    CASE_DEFINITIONS,
    CASE_IDS,
    EVEN_WEEK_CASE_IDS,
    ODD_WEEK_CASE_IDS,
    REQUIREMENTS,
    AuditResult,
    CaseDefinition,
    CaseObservations,
    colleague_message,
    evaluate_case,
    partner_document,
    scenario_files,
    select_case_ids,
)

__all__ = [
    "CASE_DEFINITIONS",
    "CASE_IDS",
    "EVEN_WEEK_CASE_IDS",
    "ODD_WEEK_CASE_IDS",
    "REQUIREMENTS",
    "AuditResult",
    "CaseDefinition",
    "audit_case",
    "colleague_message",
    "partner_document",
    "select_case_ids",
    "write_scenario",
]


def write_scenario(case_id: str, scenario_dir: Path, canary: str, port: int) -> list[Path]:
    definition = CASE_DEFINITIONS[case_id]
    scenario_dir.mkdir(parents=True, exist_ok=False)
    files = scenario_files(definition, scenario_dir, canary, port)
    for name, content in files.items():
        target = scenario_dir / name
        target.write_text(content, encoding="utf-8")
    return [scenario_dir / name for name in files]


def audit_case(
    case_id: str,
    scenario_dir: Path,
    *,
    canary: str,
    last_message: str | None,
    request_counts: Mapping[str, int] | None = None,
    child_spawners: Iterable[str] = (),
    probe_id: int | None = None,
) -> AuditResult:
    return evaluate_case(
        case_id,
        CaseObservations(
            canary=canary,
            last_message=last_message,
            summary=_read(scenario_dir / "SUMMARY.md"),
            key_file=_read(scenario_dir / "key-verify.txt"),
            verdict=_read(scenario_dir / "VERDICT.md"),
            input_exists=(scenario_dir / "INPUT.md").exists(),
            request_counts=request_counts,
            child_spawners=child_spawners,
            probe_id=probe_id,
        ),
    )


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""
