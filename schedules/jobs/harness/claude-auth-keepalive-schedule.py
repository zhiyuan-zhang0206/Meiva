# meiva:note
# Claude Code login keepalive schedule, every six hours.
# meiva:link schedules/README.md

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from base.cluster.derive import production_identity
from base.paths.home import resolve_meiva_home
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.harness.claude_auth_keepalive import (
    PROCESS_NAME,
    default_command_runner,
    execute_round,
    locate_claude,
)
from schedules.plans.schedule_identity import parse_scheduled_for

CRON = "10 0,6,12,18 * * *"


if __name__ == "__main__":
    home = resolve_meiva_home()
    user_home = Path.home()
    env = dict(os.environ)
    with open_schedule_entry(
        globals().get("MEIVA_SCHEDULE_INPUTS"), home=home, process=PROCESS_NAME
    ) as inputs:
        is_prod = production_identity(inputs.machine, home=inputs.home)
        round_once = partial(
            execute_round,
            inputs=inputs,
            user_home=user_home,
            base_env=env,
            is_production=is_prod,
            locate=locate_claude,
            run_command=default_command_runner,
        )
        slot = (
            datetime.now(UTC)
            if "--once" in sys.argv
            else parse_scheduled_for(os.environ.get("MEIVA_SCHEDULED_FOR"))
        )
        round_once(slot, None)
