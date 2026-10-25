# meiva:link schedules/docs/debt-sweep.meiva.okf.md

import os
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import base
from base.paths.home import resolve_meiva_home
from schedules.jobs.catchup import cluster_timezone
from schedules.jobs.debt_sweep import DebtSweep, dry_run, parse_cli, sweep_day
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.plans.debt_sweep_worker import parse_worker_label
from schedules.plans.directory_plan import report_recipient
from schedules.plans.schedule_identity import parse_schedule_id

CRON = "30 6 * * *"
_WORKER_LABEL_ENV = "MEIVA_DEBT_SWEEP_AGENT_LABEL"
_REPORT_AGENT_ENV = "MEIVA_DEBT_SWEEP_REPORT_AGENT"
_REPORT_LABEL = "Meiva \u8d1f\u8d23\u4eba"
_REPO_ROOT = Path(base.__file__).resolve().parents[1]


if __name__ == "__main__":
    cli = parse_cli(sys.argv[1:])
    schedule = CronSchedule((Trigger(CRON),), timezone=cluster_timezone())
    sweep = DebtSweep(
        repo=(cli.repo or _REPO_ROOT).resolve(),
        home=resolve_meiva_home(),
        tz=schedule.timezone,
        worker_label=parse_worker_label(os.environ.get(_WORKER_LABEL_ENV)),
    )
    now = datetime.now(UTC)
    if cli.dry_run:
        dry_run(sweep, now)
    else:
        recipient = report_recipient(
            "debt-sweep-daily",
            _REPORT_AGENT_ENV,
            _REPORT_LABEL,
            os.environ.get(_REPORT_AGENT_ENV),
        )
        with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS")) as inputs:
            run_loop(
                inputs.database(),
                schedule,
                partial(sweep_day, job=sweep, recipient=recipient, inputs=inputs),
                schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
                started_at=now,
            )
