# meiva:note
# The daily tech-debt sweep: run the mechanical scan, keep its report, wake the clearing worker
# with the report as evidence, and emit the day's event.
#
# `run_sweep` is the working half and raises on any failure; `sweep_day` wraps it with the
# telemetry event and the failure report to the P0 lead. The scan is a bash program whose whole
# process tree is killed when it outlives its bound. A claimed slot that fails is not retried:
# the lead's message names the manual pass. `--once --dry-run` runs the scan and prints what a
# real run would send, without a claim, an agent call, telemetry or a database.
# meiva:link schedules/docs/debt-sweep.meiva.okf.md

from __future__ import annotations

import argparse
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from base.daemon.schedules.inputs import ScheduleInputs
from base.host.proc import run_bounded
from schedules.jobs.agent_directory import AgentDispatch, ensure_agent, notify_report_agent
from schedules.jobs.telemetry_event import emit_system_event
from schedules.plans.debt_sweep_scan import (
    ScanExited,
    ScanOutcome,
    ScanReport,
    ScanUnrunnable,
    scan_artifact_path,
    scan_report,
    slot_day,
)
from schedules.plans.debt_sweep_worker import (
    dry_run_text,
    event_payload,
    failure_message,
    log_line,
    worker_prompt,
)
from schedules.plans.directory_plan import ReportRecipient

__all__ = [
    "DebtSweep",
    "SweepCli",
    "SweepDone",
    "dry_run",
    "parse_cli",
    "run_scan",
    "run_sweep",
    "sweep_day",
]

_PROCESS_NAME = "schedule-debt-sweep-daily"
_SCAN_PROGRAM = ("scripts", "audit", "tech_debt_candidates.sh")


# meiva:note
# What one sweep needs, read once by the entry: the repository the scan inspects, the home the
# reports are stored under, the cluster timezone, the label of the clearing worker and the bound
# on the scan (a tuning constant a test shortens).
@dataclass(frozen=True)
class DebtSweep:
    repo: Path
    home: Path
    tz: str
    worker_label: str
    scan_timeout: float = 15 * 60.0


@dataclass(frozen=True)
class SweepDone:
    day: str
    artifact: Path
    scan: ScanReport
    dispatch: AgentDispatch


# meiva:note
# `repo` is only meaningful with `--once --dry-run`, the manual demonstration. The scheduler runs
# the script with no flags.
@dataclass(frozen=True)
class SweepCli:
    dry_run: bool
    repo: Path | None


def parse_cli(argv: Sequence[str]) -> SweepCli:
    parser = argparse.ArgumentParser(description="Run the daily tech-debt clearing schedule.")
    parser.add_argument("--once", action="store_true", help="run one dry-run demonstration")
    parser.add_argument("--dry-run", action="store_true", help="scan without schedule side effects")
    parser.add_argument("--repo", type=Path, help="repository root for --once --dry-run")
    args = parser.parse_args(argv)
    if args.dry_run and not args.once:
        parser.error("--dry-run requires --once")
    if args.once and not args.dry_run:
        parser.error("--once requires --dry-run")
    if args.repo is not None and not args.dry_run:
        parser.error("--repo requires --once --dry-run")
    return SweepCli(bool(args.dry_run), args.repo)


def run_scan(repo: Path, timeout: float) -> ScanOutcome:
    script = repo.joinpath(*_SCAN_PROGRAM)
    try:
        done = run_bounded(
            ["bash", str(script), "--repo", str(repo)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ScanUnrunnable(f"{type(exc).__name__}: {exc}")
    return ScanExited(done.returncode, done.stdout + done.stderr)


def run_sweep(slot: datetime, job: DebtSweep) -> SweepDone:
    day = slot_day(slot, job.tz)
    artifact = scan_artifact_path(job.home, day, slot)
    scan = scan_report(run_scan(job.repo, job.scan_timeout))
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(scan.text, encoding="utf-8")
    dispatch = ensure_agent(job.worker_label, worker_prompt(day, artifact, scan))
    return SweepDone(day, artifact, scan, dispatch)


def sweep_day(
    slot: datetime,
    _payload: str | None,
    *,
    job: DebtSweep,
    recipient: ReportRecipient,
    inputs: ScheduleInputs,
) -> None:
    try:
        done = run_sweep(slot, job)
        emit_system_event(
            "debt_sweep_daily",
            event_payload(done.day, done.scan, done.dispatch.action, done.dispatch.agent_id),
            process=_PROCESS_NAME,
            producer=inputs.producer,
            machine_reader=inputs.machine.name,
            image=inputs.image,
        )
        print(log_line(slot, done.day, done.scan, done.dispatch.action, done.dispatch.agent_id))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        print(f"debt-sweep-daily failed: {detail}")
        notify_report_agent(recipient, failure_message(detail))


def dry_run(job: DebtSweep, now: datetime) -> None:
    day = slot_day(now, job.tz)
    artifact = scan_artifact_path(job.home, day, now)
    scan = scan_report(run_scan(job.repo, job.scan_timeout))
    print(dry_run_text(day, artifact, scan, job.worker_label), end="")
