# meiva:link schedules/docs/c9-daily-report.md

import os

from base.host.system.hostport import Host
from base.paths.home import resolve_meiva_home
from schedules.jobs.c9_report import PROCESS_NAME, C9Report, report_day
from schedules.jobs.entry import cluster_timezone, open_schedule_entry
from schedules.plans.directory_plan import report_recipient
from schedules.plans.schedule_identity import parse_scheduled_for

CRON = "0 5 * * *"
_REPORT_AGENT_ENV = "MEIVA_CI_USAGE_REPORT_AGENT"
_REPORT_LABEL = "Meiva \u8d1f\u8d23\u4eba"


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(
        globals().get("MEIVA_SCHEDULE_INPUTS"), home=home, process=PROCESS_NAME
    ) as inputs:
        from scripts.ci.pull_requests import accounting

        job = C9Report(
            tz=cluster_timezone(),
            recipient=report_recipient(
                "c9-daily-report",
                _REPORT_AGENT_ENV,
                _REPORT_LABEL,
                os.environ.get(_REPORT_AGENT_ENV),
            ),
            repo=accounting.DEFAULT_REPO,
            ledger=accounting.DEFAULT_LEDGER,
            host=Host.system(environ=os.environ),
        )
        report_day(
            parse_scheduled_for(os.environ.get("MEIVA_SCHEDULED_FOR")),
            None,
            job=job,
            inputs=inputs,
        )
