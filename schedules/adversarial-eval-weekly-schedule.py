# meiva:link schedules/docs/adversarial-eval.md

import os
import random
from datetime import UTC, datetime

from base.paths.home import resolve_meiva_home
from schedules.jobs.adversarial_batch import BatchConfig, fire_weekly_batch
from schedules.jobs.entry import open_schedule_entry
from schedules.plans.schedule_identity import parse_scheduled_for

CRON = "0 4 * * 3"

if __name__ == "__main__":
    home = resolve_meiva_home()
    # meiva:note
    # The process entry builds the one generator this run draws from — the batch mints each case's
    # canary through it (`new_uuid(rng)`), the same `random.SystemRandom` the schedule entry binds
    # into the SDK context.
    rng = random.SystemRandom()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        config = BatchConfig(root=home / "adversarial_eval")
        fire_weekly_batch(
            parse_scheduled_for(os.environ.get("MEIVA_SCHEDULED_FOR")),
            None,
            config=config,
            clock=lambda: datetime.now(UTC),
            rng=rng,
        )
