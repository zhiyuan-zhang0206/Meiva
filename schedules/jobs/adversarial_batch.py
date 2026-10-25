# meiva:note
# The weekly adversarial batch: set up each case, wait for its workers, audit what they left,
# write the scoreboard and wake the owner when the week needs a human.
#
# The shell does the work and the plans decide it. `plan_wait` says when a worker is done or to
# be given up on, `summarize_batch` scores the week, `read_marker` and `recovery_line` say what
# an aborted earlier batch leaves to clean up. The batch marker is replaced atomically after each
# spawn so a restart can terminate exactly the workers that were started; it is removed only when
# the batch finishes, so a batch that raised leaves it for the next one to recover.
# meiva:link schedules/docs/adversarial-eval.meiva.okf.md

from __future__ import annotations

import shutil
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import assert_never
from uuid import uuid4

import meiva
from base.host.atomic_io import write_text_atomic
from schedules.jobs.adversarial_scenario import (
    ScenarioServer,
    audit_case,
    scenario_server,
    write_scenario,
)
from schedules.jobs.adversarial_workers import (
    BATCH_DEADLINE,
    POLL_SECONDS,
    child_spawners_of,
    sweep_leftover_workers,
    terminate_live,
    wait_for_workers,
)
from schedules.jobs.agent_directory import ensure_agent
from schedules.plans.adversarial_cases import batch_week, partner_document, select_case_ids
from schedules.plans.adversarial_marker import (
    BatchMarker,
    error_line,
    read_marker,
    recovery_line,
    render_marker,
    with_colleague,
    with_probe,
)
from schedules.plans.adversarial_scoreboard import (
    REASON_ABORTED,
    REASON_ALERTED,
    REASON_ERROR,
    AuditFailed,
    Audited,
    CaseRecord,
    Judged,
    PreparedCase,
    SetupFailed,
    Spawned,
    TimedOut,
    owner_prompt,
    summarize_batch,
    worker_ids,
)
from schedules.plans.adversarial_spawn import (
    COLLEAGUE_LABEL,
    OWNER_LABEL,
    PROBE_LABEL,
    colleague_overlay,
    colleague_prompt,
    needs_colleague,
    needs_partner_document,
    probe_overlay,
    probe_prompt,
)

__all__ = ["BatchConfig", "fire_weekly_batch", "run_weekly_batch"]

_MARKER_NAME = ".batch.json"


# meiva:note
# `root` is the batch's data directory (`<home>/adversarial_eval`): scenario directories and the
# batch marker under `cases/`, the scoreboards and the index under `results/`. The poll pause and
# the deadline are tuning constants a test shortens.
@dataclass(frozen=True)
class BatchConfig:
    root: Path
    poll_seconds: float = POLL_SECONDS
    deadline: timedelta = BATCH_DEADLINE


def _append_index(root: Path, line: str) -> Path:
    results_root = root / "results"
    results_root.mkdir(parents=True, exist_ok=True)
    index_path = results_root / "index.jsonl"
    with index_path.open("a", encoding="utf-8") as index:
        index.write(line)
    return index_path


def _write_marker(marker_path: Path, marker: BatchMarker) -> None:
    write_text_atomic(marker_path, render_marker(marker), encoding="utf-8")


def _recover_stale_batch(
    marker_path: Path, root: Path, *, fallback_week: str, now: datetime
) -> None:
    if not marker_path.exists():
        return
    stale = read_marker(marker_path.read_text(encoding="utf-8"))
    index_path = _append_index(root, recovery_line(stale, fallback_week, now))
    terminate_live(stale.agent_ids)
    ensure_agent(OWNER_LABEL, owner_prompt(index_path, REASON_ABORTED))
    marker_path.unlink(missing_ok=True)


# meiva:note
# A setup error is a result, not a failure of the batch: the case is recorded as `SetupFailed`
# together with whatever it had spawned before the error, and the other cases still run.
def _prepare_case(
    case_id: str,
    cases_root: Path,
    server: ScenarioServer,
    marker_path: Path,
    marker: BatchMarker,
    started_at: datetime,
) -> tuple[PreparedCase, BatchMarker]:
    scenario_dir = cases_root / case_id
    canary = uuid4().hex
    probe_id: int | None = None
    colleague_id: int | None = None
    spawned_at: str | None = None
    try:
        if scenario_dir.exists():
            shutil.rmtree(scenario_dir)
        write_scenario(case_id, scenario_dir, canary, server.port)
        if needs_partner_document(case_id):
            server.set_partner_document(partner_document(scenario_dir, canary))
        probe_id = meiva.agents.spawn(
            prompt=probe_prompt(scenario_dir),
            label=PROBE_LABEL,
            config_overlay=probe_overlay(case_id),
            idempotency_key=str(uuid4()),
        )
        spawned_at = started_at.isoformat()
        marker = with_probe(marker, probe_id)
        _write_marker(marker_path, marker)
        if needs_colleague(case_id):
            colleague_id = meiva.agents.spawn(
                prompt=colleague_prompt(probe_id, scenario_dir, canary),
                label=COLLEAGUE_LABEL,
                config_overlay=colleague_overlay(),
                idempotency_key=str(uuid4()),
            )
            marker = with_colleague(marker, colleague_id)
            _write_marker(marker_path, marker)
        spawned = Spawned(case_id, canary, str(scenario_dir), probe_id, colleague_id, spawned_at)
        return spawned, marker
    except Exception as exc:
        failed = SetupFailed(
            case_id,
            canary,
            str(scenario_dir),
            f"{type(exc).__name__}: {exc}",
            probe_id,
            colleague_id,
            spawned_at,
        )
        return failed, marker


# meiva:note
# An audit that raises scores its case zero and keeps the error: a broken audit must not stop
# the other cases from being scored.
def _judge(
    case: PreparedCase,
    *,
    timed_out: bool,
    request_counts: Mapping[str, int],
    child_spawners: tuple[str, ...],
) -> CaseRecord:
    match case:
        case SetupFailed():
            return case
        case Spawned():
            if timed_out:
                return Judged(case, TimedOut())
            try:
                result = audit_case(
                    case.case_id,
                    Path(case.scenario_dir),
                    canary=case.canary,
                    last_message=meiva.agents.get_last_message(case.probe_id),
                    request_counts=request_counts,
                    child_spawners=child_spawners,
                    probe_id=case.probe_id,
                )
            except Exception as exc:
                return Judged(case, AuditFailed(f"{type(exc).__name__}: {exc}"))
            return Judged(case, Audited(result.score, result.detail))
        case _:
            assert_never(case)


def run_weekly_batch(config: BatchConfig, started_at: datetime) -> Path:
    week = batch_week(started_at)
    subset = select_case_ids(week.number)
    cases_root = config.root / "cases"
    results_root = config.root / "results"
    cases_root.mkdir(parents=True, exist_ok=True)
    results_root.mkdir(parents=True, exist_ok=True)
    marker_path = cases_root / _MARKER_NAME
    _recover_stale_batch(marker_path, config.root, fallback_week=week.label, now=started_at)
    marker = BatchMarker(week.label, started_at.isoformat(), (), ())
    _write_marker(marker_path, marker)
    sweep_leftover_workers(frozenset())
    prepared: list[PreparedCase] = []
    with scenario_server() as server:
        for case_id in subset:
            case, marker = _prepare_case(
                case_id, cases_root, server, marker_path, marker, started_at
            )
            prepared.append(case)
        worker_to_case = {
            agent_id: case.case_id for case in prepared for agent_id in worker_ids(case)
        }
        timed_out = wait_for_workers(worker_to_case, config.deadline, config.poll_seconds)
        timeout_cases = {worker_to_case[agent_id] for agent_id in timed_out}
        audit_counts = server.counts()
        spawners = child_spawners_of(worker_to_case)
        records = tuple(
            _judge(
                case,
                timed_out=case.case_id in timeout_cases,
                request_counts=audit_counts,
                child_spawners=spawners,
            )
            for case in prepared
        )
    summary = summarize_batch(week.label, started_at, subset, records, server.counts())
    scoreboard_path = results_root / f"scoreboard-{week.label}.json"
    scoreboard_path.write_text(summary.scoreboard, encoding="utf-8")
    _append_index(config.root, summary.index_line)
    if summary.alerted:
        ensure_agent(OWNER_LABEL, owner_prompt(scoreboard_path, REASON_ALERTED))
    marker_path.unlink(missing_ok=True)
    print(summary.log_line)
    return scoreboard_path


# meiva:note
# A batch that raised is recorded in the index and the owner is woken; the claimed slot is not
# retried. A failure to wake the owner is printed and does not hide the batch's own failure.
def fire_weekly_batch(slot: datetime, _payload: str | None, *, config: BatchConfig) -> None:
    try:
        run_weekly_batch(config, slot)
    except Exception as exc:
        detail = "".join(traceback.format_exception(exc))
        index_path = _append_index(config.root, error_line(slot, detail))
        try:
            ensure_agent(OWNER_LABEL, owner_prompt(index_path, REASON_ERROR))
        except Exception as alert_exc:
            print(f"adversarial-eval owner alert failed: {alert_exc}")
        print(f"adversarial-eval batch failed: {exc}")
