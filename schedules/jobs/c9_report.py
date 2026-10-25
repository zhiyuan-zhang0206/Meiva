# meiva:note
# The daily CI-minute reconciliation: collect the claimed slot's 24 hours of CI runs, append
# them to the attribution ledger, emit the day's totals, and wake the P0 lead on failure.
#
# `reconcile_window` is the reading half and touches neither logging nor telemetry; the report
# half emits `ci_usage_daily` and prints. Idempotency has two layers: the slot claim is
# at-most-once and the ledger append is keyed by run id, so re-running a window appends
# nothing twice. A failed reconciliation does not retry; its message names the manual backfill.
# `accounting` is imported where it is used: a script runs from the repository's installed
# roots, not from a checkout, so the import resolves when the schedule fires.
# meiva:link schedules/docs/c9-daily-report.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from base.daemon.schedules.inputs import ScheduleInputs
from base.host.system.hostport import Host
from schedules.jobs.agent_directory import notify_report_agent
from schedules.jobs.telemetry_event import emit_system_event
from schedules.plans.c9_window import CiRun, MinuteRates, failure_message, summarize, window_bounds
from schedules.plans.directory_plan import ReportRecipient

__all__ = ["C9Report", "DayReconciled", "reconcile_window", "report_day"]

_PROCESS_NAME = "schedule-c9-daily"


# meiva:note
# What one C9 run needs, read once by the entry: the cluster timezone, who hears about a
# failure, the repository and ledger the reconciliation covers, and the host that runs `gh`.
@dataclass(frozen=True)
class C9Report:
    tz: str
    recipient: ReportRecipient
    repo: str
    ledger: Path
    host: Host


@dataclass(frozen=True)
class DayReconciled:
    day: str
    runs: int
    appended: int
    attributes: Mapping[str, str | int | float]


def reconcile_window(slot_end: datetime, job: C9Report) -> DayReconciled:
    from scripts.ci.pull_requests import accounting

    since, until, day = window_bounds(slot_end, job.tz)
    entries = accounting.collect(job.repo, since, until, host=job.host)
    appended = accounting.append_ledger(job.ledger, entries)
    runs = tuple(
        CiRun(
            agent_id=entry["agent_id"] if isinstance(entry.get("agent_id"), int) else None,
            linux_minutes=int(entry.get("linux_minutes") or 0),
            macos_minutes=int(entry.get("macos_minutes") or 0),
        )
        for entry in entries
    )
    rates = MinuteRates(accounting.LINUX_MINUTE_USD, accounting.MACOS_MINUTE_USD)
    attributes = summarize(
        runs,
        day=day,
        window_start=since,
        window_end=until,
        appended_runs=appended,
        rates=rates,
    )
    return DayReconciled(day, len(entries), appended, attributes)


def report_day(
    slot_end: datetime, _payload: str | None, *, job: C9Report, inputs: ScheduleInputs
) -> None:
    try:
        done = reconcile_window(slot_end, job)
        emit_system_event(
            "ci_usage_daily",
            done.attributes,
            process=_PROCESS_NAME,
            producer=inputs.producer,
            machine_reader=inputs.machine.name,
            image=inputs.image,
        )
        print(
            f"[{slot_end.isoformat()}] c9-daily-report: {done.day} — "
            f"{done.runs} runs, {done.appended} appended"
        )
    except Exception as exc:
        print(f"c9-daily-report failed: {exc}")
        notify_report_agent(job.recipient, failure_message(f"{type(exc).__name__}: {exc}"))
