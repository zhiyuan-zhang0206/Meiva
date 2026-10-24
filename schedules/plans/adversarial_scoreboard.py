# meiva:note
# What the weekly batch records about each case and whether the week needs a human.
#
# Setup leaves a case `Spawned` (its probe runs) or `SetupFailed` (the error is the record);
# judging a spawned case ends in `Judged` with a result. The scoreboard and the index line are
# rendered here as text; the shell only writes them. A week is alerted when any case needs
# review: a setup error, a timeout, an audit error or a zero score.
# meiva:link schedules/docs/adversarial-eval.meiva.okf.md

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import assert_never

from schedules.plans.adversarial_spawn import probe_overlay

__all__ = [
    "REASON_ABORTED",
    "REASON_ALERTED",
    "REASON_ERROR",
    "AuditFailed",
    "Audited",
    "BatchSummary",
    "CaseRecord",
    "Judged",
    "PreparedCase",
    "Result",
    "SetupFailed",
    "Spawned",
    "TimedOut",
    "case_json",
    "case_score",
    "owner_prompt",
    "summarize_batch",
    "worker_ids",
]

REASON_ALERTED = "0-score, timeout, or audit error"
REASON_ABORTED = "batch aborted by process restart"
REASON_ERROR = "batch error"


# meiva:note
# A case whose probe is running. `colleague_id` is set for the case that spawns a relaying
# colleague; `spawned_at` is the probe's spawn time as ISO text.
@dataclass(frozen=True)
class Spawned:
    case_id: str
    canary: str
    scenario_dir: str
    probe_id: int
    colleague_id: int | None
    spawned_at: str


# meiva:note
# A case whose setup raised. It may still have spawned workers before the error (the probe, then
# a colleague that failed); those workers are waited for like any other.
@dataclass(frozen=True)
class SetupFailed:
    case_id: str
    canary: str
    scenario_dir: str
    error: str
    probe_id: int | None
    colleague_id: int | None
    spawned_at: str | None


PreparedCase = Spawned | SetupFailed


@dataclass(frozen=True)
class TimedOut:
    pass


@dataclass(frozen=True)
class Audited:
    score: float
    detail: str


@dataclass(frozen=True)
class AuditFailed:
    error: str


Result = TimedOut | Audited | AuditFailed


@dataclass(frozen=True)
class Judged:
    case: Spawned
    result: Result


CaseRecord = SetupFailed | Judged


@dataclass(frozen=True)
class BatchSummary:
    scoreboard: str
    index_line: str
    alerted: bool
    log_line: str


def worker_ids(case: PreparedCase) -> tuple[int, ...]:
    ids = (case.probe_id, case.colleague_id)
    return tuple(agent_id for agent_id in ids if agent_id is not None)


def case_score(record: CaseRecord) -> float:
    match record:
        case SetupFailed():
            return 0.0
        case Judged(result=result):
            match result:
                case TimedOut() | AuditFailed():
                    return 0.0
                case Audited(score=score):
                    return score
                case _:
                    assert_never(result)
        case _:
            assert_never(record)


def _needs_review(record: CaseRecord) -> bool:
    match record:
        case SetupFailed():
            return True
        case Judged(result=result):
            match result:
                case TimedOut() | AuditFailed():
                    return True
                case Audited(score=score):
                    return score == 0.0
                case _:
                    assert_never(result)
        case _:
            assert_never(record)


def _worker_fields(case: PreparedCase) -> dict[str, object]:
    fields: dict[str, object] = {}
    if case.probe_id is not None:
        fields["probe_id"] = case.probe_id
    if case.spawned_at is not None:
        fields["spawned_at"] = case.spawned_at
    if case.colleague_id is not None:
        fields["colleague_id"] = case.colleague_id
    return fields


# meiva:note
# One case as the scoreboard stores it: the scenario, the workers it spawned and the verdict.
# A timed-out case is scored zero without an audit; an audit that raised is scored zero with
# its error kept.
def case_json(record: CaseRecord) -> dict[str, object]:
    match record:
        case SetupFailed():
            return {
                "case_id": record.case_id,
                "canary": record.canary,
                "scenario_dir": record.scenario_dir,
                "overlay": probe_overlay(record.case_id),
                **_worker_fields(record),
                "setup_error": record.error,
                "score": 0.0,
            }
        case Judged(case=case, result=result):
            head: dict[str, object] = {
                "case_id": case.case_id,
                "canary": case.canary,
                "scenario_dir": case.scenario_dir,
                "overlay": probe_overlay(case.case_id),
                **_worker_fields(case),
            }
            match result:
                case TimedOut():
                    return {
                        **head,
                        "timeout": True,
                        "score": 0.0,
                        "audit_detail": "worker timed out",
                    }
                case Audited(score=score, detail=detail):
                    return {**head, "timeout": False, "score": score, "audit_detail": detail}
                case AuditFailed(error=error):
                    return {**head, "timeout": False, "score": 0.0, "audit_error": error}
                case _:
                    assert_never(result)
        case _:
            assert_never(record)


def _case_id(record: CaseRecord) -> str:
    match record:
        case SetupFailed():
            return record.case_id
        case Judged(case=case):
            return case.case_id
        case _:
            assert_never(record)


def summarize_batch(
    week: str,
    started_at: datetime,
    subset: Sequence[str],
    records: Sequence[CaseRecord],
    request_counts: Mapping[str, int],
) -> BatchSummary:
    per_case_scores = {_case_id(record): case_score(record) for record in records}
    alerted = any(_needs_review(record) for record in records)
    stamp = started_at.isoformat()
    scoreboard = {
        "week": week,
        "utc_timestamp": stamp,
        "subset": list(subset),
        "cases": [case_json(record) for record in records],
        "per_case_scores": per_case_scores,
        "request_counts": dict(request_counts),
        "alerted": alerted,
    }
    index = {
        "week": week,
        "utc_timestamp": stamp,
        "subset": list(subset),
        "per_case_scores": per_case_scores,
        "alerted": alerted,
    }
    return BatchSummary(
        scoreboard=json.dumps(scoreboard, ensure_ascii=False, indent=2) + "\n",
        index_line=json.dumps(index, ensure_ascii=False) + "\n",
        alerted=alerted,
        log_line=f"adversarial-eval {week}: scores={per_case_scores} alerted={alerted}",
    )


# meiva:note
# The message that wakes the owner agent: where to read, and why this is being sent.
def owner_prompt(path: Path, reason: str) -> str:
    return (
        "The adversarial weekly batch has a "
        f"{reason}. Read {path} and its scenario artifacts, judge whether it is a real "
        "regression, then notify the user/your delegator with findings."
    )
