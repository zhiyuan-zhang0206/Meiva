# meiva:note
# Daily external harness CLI update schedule.
# meiva:link schedules/README.md

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from base.cluster.derive import production_identity
from base.paths.home import resolve_meiva_home
from schedules.jobs.entry import cluster_timezone, open_schedule_entry
from schedules.jobs.harness.harness_cli_update import default_shell_runner, execute_round
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.plans.schedule_identity import parse_schedule_id

CRON = "30 4 * * *"


if __name__ == "__main__":
    home = resolve_meiva_home()
    user_home = Path.home()
    env = dict(os.environ)
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        is_prod = production_identity(inputs.machine, home=inputs.home)
        round_once = partial(
            execute_round,
            inputs=inputs,
            user_home=user_home,
            base_env=env,
            is_production=is_prod,
            run_shell=default_shell_runner,
        )
        if "--once" in sys.argv:
            round_once(datetime.now(UTC), None)
            raise SystemExit(0)
        run_loop(
            inputs.database(),
            CronSchedule((Trigger(CRON),), timezone=cluster_timezone()),
            round_once,
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
            clock=lambda: datetime.now(UTC),
        )
