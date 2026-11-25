# meiva:link schedules/docs/debt-sweep.md

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import base
from base.paths.home import resolve_meiva_home
from schedules.jobs.debt_sweep import PROCESS_NAME, DebtSweep, dry_run, parse_cli, sweep_day
from schedules.jobs.entry import cluster_timezone, open_schedule_entry
from schedules.plans.debt_sweep_worker import parse_worker_label
from schedules.plans.directory_plan import report_recipient
from schedules.plans.schedule_identity import parse_scheduled_for

CRON = "30 6 * * *"
_WORKER_LABEL_ENV = "MEIVA_DEBT_SWEEP_AGENT_LABEL"
_REPORT_AGENT_ENV = "MEIVA_DEBT_SWEEP_REPORT_AGENT"
_REPORT_LABEL = "Meiva \u8d1f\u8d23\u4eba"
_REPO_ROOT = Path(base.__file__).resolve().parents[1]


if __name__ == "__main__":
    cli = parse_cli(sys.argv[1:])
    home = resolve_meiva_home()
    sweep = DebtSweep(
        repo=(cli.repo or _REPO_ROOT).resolve(),
        home=home,
        tz=cluster_timezone(),
        worker_label=parse_worker_label(os.environ.get(_WORKER_LABEL_ENV)),
    )
    slot = (
        datetime.now(UTC)
        if cli.dry_run
        else parse_scheduled_for(os.environ.get("MEIVA_SCHEDULED_FOR"))
    )
    if cli.dry_run:
        dry_run(sweep, slot)
    else:
        recipient = report_recipient(
            "debt-sweep-daily",
            _REPORT_AGENT_ENV,
            _REPORT_LABEL,
            os.environ.get(_REPORT_AGENT_ENV),
        )
        with open_schedule_entry(
            globals().get("MEIVA_SCHEDULE_INPUTS"), home=home, process=PROCESS_NAME
        ) as inputs:
            sweep_day(slot, None, job=sweep, recipient=recipient, inputs=inputs)
