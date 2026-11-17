# meiva:link schedules/README.md

import os
import subprocess

from base.clock import SYSTEM_TIMERS
from base.paths.home import resolve_meiva_home
from schedules.jobs.watch.ci_metrics import CiMetrics, collect_ci_metrics
from schedules.jobs.entry import cluster_timezone, open_schedule_entry
from schedules.plans.ci_workflows import WatchedWorkflow
from schedules.plans.directory_plan import report_recipient
from schedules.plans.schedule_identity import parse_scheduled_for

CRON = "20 6 * * *"
_REPORT_AGENT_ENV = "MEIVA_CI_METRICS_REPORT_AGENT"
_REPORT_LABEL = "Meiva \u8d1f\u8d23\u4eba"

# meiva:note
# The timed workflows a pull request never blocks. A red one is silent by default, so each
# entry carries what stays wrong while it fails: the alert has to say what it costs.
_SCHEDULED_WORKFLOWS = (
    WatchedWorkflow(
        "refresh-test-durations.yml",
        "CI keeps splitting shards with the previous test-duration model",
    ),
    WatchedWorkflow(
        "update-model-pricing.yml",
        "the price catalog stops following its sources, so every per-model cost figure "
        "drifts from what the vendor charges",
    ),
)


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        job = CiMetrics(
            tz=cluster_timezone(),
            recipient=report_recipient(
                "dev-ci-metrics",
                _REPORT_AGENT_ENV,
                _REPORT_LABEL,
                os.environ.get(_REPORT_AGENT_ENV),
            ),
            watched=_SCHEDULED_WORKFLOWS,
            state_dir=home / "state" / "ci-runs",
            run_process=subprocess.run,
            timers=SYSTEM_TIMERS,
        )
        collect_ci_metrics(
            parse_scheduled_for(os.environ.get("MEIVA_SCHEDULED_FOR")),
            None,
            job=job,
            inputs=inputs,
        )
