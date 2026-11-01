# meiva:note
# A fresh schedule entry retains the loaded image and one admission gate for database handles instead of relabeling code after checkout changes.
# meiva:link schedules/docs/entry-composition.meiva.okf.md

import random
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from base.agents.context import MeivaContext, deferred_event_bus
from base.agents.context.clients import ClientSet
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.daemon.schedules.inputs import ScheduleInputs, schedule_entry
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.log import prepare_process_logging
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import build_pipeline, process_name
from meiva.sdk_surface import process_context

__all__ = ["fresh_schedule_entry", "open_schedule_entry", "schedule_entry"]


# meiva:note
# The cluster wall clock (`MEIVA_TIMEZONE`, cluster-pinned), never the host's OS timezone: the fleet
# fires at one instant wherever each machine sits, and a weekly cron read host-locally lands the run
# on the wrong calendar day, not merely at the wrong hour. An entry reads it once and hands the value
# down, so a changed value waits for the schedule's next restart.
# meiva:link schedules/README.md
def cluster_timezone() -> str:
    return Clock.from_settings().timezone


# meiva:note
# `image` is the commit the process loaded and `process` names it in the database gate; `home` is
# the home the script's entry resolved. All three are the caller's values: the entry reads none
# of them from the checkout it happens to sit in.
@contextmanager
def fresh_schedule_entry(
    *, image: LoadedCommit, process: str, home: Path
) -> Generator[ScheduleInputs]:
    import meiva

    config = ConfigBoot()
    config.boot()
    machine = ConfiguredMachineIdentity(config)
    gate = ProcessDbGate(process=process, version=CodeVersion(image).get)

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=home),
        )

    clients = ClientSet(
        database=database,
        pipeline_factory=lambda: build_pipeline(database=database, machine=machine, home=home),
    )
    prior = getattr(meiva, "context", None)
    rng = random.SystemRandom()
    process_context.bind_context(
        MeivaContext(
            clients=clients,
            machine=machine,
            clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
            meiva_home=home,
            user_home=Path.home(),
            environ=config.process_environment(),
            rng=rng,
            bus_factory=deferred_event_bus(config, rng=rng, timers=SYSTEM_TIMERS),
        )
    )
    try:
        yield ScheduleInputs(database, clients.event_pipeline, image, machine=machine, home=home)
    except BaseException as primary:
        try:
            clients.close(pipeline_timeout=2)
        except BaseException as cleanup:
            primary.add_note(f"schedule entry cleanup failed: {cleanup!r}")
        raise
    else:
        clients.close(pipeline_timeout=2)
    finally:
        if prior is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(prior)


# meiva:note
# A script's `__main__` hands over `MEIVA_SCHEDULE_INPUTS` (what the runner injected, or `None`
# when the script is run by hand). Inputs from a runner are borrowed as they are; a hand run
# opens process logging and owns a fresh entry for the image this process loaded.
def open_schedule_entry(
    borrowed: ScheduleInputs | None, *, home: Path
) -> AbstractContextManager[ScheduleInputs]:
    if borrowed is not None:
        return schedule_entry(borrowed)
    prepare_process_logging()
    return fresh_schedule_entry(
        image=process_context.loaded_code_image(), process=process_name(), home=home
    )
