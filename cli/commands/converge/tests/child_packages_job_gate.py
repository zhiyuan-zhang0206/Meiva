# meiva:note
# A fresh operator owner consumes actual process delivery. Only the command runner is the
# existing base crontab resource; no registrar or config reader is replaced.
from __future__ import annotations

import json
import random
from pathlib import Path

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.host.system.tests.fake_commands import fake_host
from base.host.system.tests.fake_crontab import FakeCrontab
from base.paths import meiva_home
from base.sessions.tests.refuse_helper import refuse_helper
from cli.commands.converge.os_jobs import ensure_packages_refresh_job
from cli.commands.converge.spec import ConvergeCtx
from cli.database import (
    open_operator_database,
    operator_database_factory,
    operator_event_pipeline,
)
from cli.database.runtime import UserSession


def main() -> None:
    config = ConfigBoot()
    config.read_process_environment()
    machine = ConfiguredMachineIdentity(config)
    crontab = FakeCrontab()
    host = fake_host(crontab, programs={"crontab": "/usr/bin/crontab"})
    home = meiva_home()
    database = operator_database_factory(
        local_host=machine.host, home=home, open_database=open_operator_database
    )
    pipeline = operator_event_pipeline(database, machine=machine, home=home)
    context = ConvergeCtx(
        secure_rng=random.SystemRandom(),
        repo=Path(__file__).resolve().parents[4],
        meiva_home=home,
        roles=None,
        config=config,
        database_factory=database,
        producer=pipeline,
        machine=machine,
        helper=refuse_helper,
        host=host,
        session=UserSession(user_home=Path.home(), login_shell="/bin/sh", interactive=False),
        services=frozenset(),
        timers=ManualTimers(),
        clock=Clock(
            ClockConfig("UTC", None, message_timestamp_weekday=False), timers=ManualTimers()
        ),
    )
    try:
        ensure_packages_refresh_job(context)
        print(json.dumps({"upgrades": config.upgrades, "crontab": crontab.table}))
    finally:
        pipeline.close_command(primary=None)


if __name__ == "__main__":
    main()
