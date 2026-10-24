# meiva:link schedules/docs/c9-daily-report.meiva.okf.md

import os
from datetime import UTC, datetime
from functools import partial

from base.host.system.hostport import Host
from schedules.jobs.c9_report import C9Report, report_day
from schedules.jobs.catchup import cluster_timezone
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.plans.directory_plan import report_recipient
from schedules.plans.schedule_identity import parse_schedule_id

CRON = "0 5 * * *"
_REPORT_AGENT_ENV = "MEIVA_CI_USAGE_REPORT_AGENT"
_REPORT_LABEL = "Meiva \u8d1f\u8d23\u4eba"


if __name__ == "__main__":
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS")) as inputs:
        from scripts.ci.pull_requests import accounting

        schedule = CronSchedule((Trigger(CRON),), timezone=cluster_timezone())
        job = C9Report(
            tz=schedule.timezone,
            recipient=report_recipient(
                "c9-daily-report",
                _REPORT_AGENT_ENV,
                _REPORT_LABEL,
                os.environ.get(_REPORT_AGENT_ENV),
            ),
            repo=accounting.DEFAULT_REPO,
            ledger=accounting.DEFAULT_LEDGER,
            host=Host.system(),
        )
        run_loop(
            inputs.database(),
            schedule,
            partial(report_day, job=job, inputs=inputs),
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
        )
