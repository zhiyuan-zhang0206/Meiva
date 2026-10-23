# meiva:note
# A fresh schedule entry retains the loaded image and one admission gate for database handles instead of relabeling code after checkout changes.
# meiva:link schedules/docs/entry-composition.meiva.okf.md

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.clock import Clock, SYSTEM_TIMERS
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.paths.home import resolve_meiva_home
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import build_pipeline, process_name
from base.daemon.schedules.inputs import ScheduleInputs, schedule_entry
from meiva.sdk_surface import process_context

__all__ = ["ScheduleEntryHooks", "fresh_schedule_entry", "schedule_entry"]


@dataclass(frozen=True)
class ScheduleEntryHooks:
    image_loader: Callable[[], LoadedCommit] = field(
        default_factory=lambda: process_context.loaded_code_image
    )
    process_name_fn: Callable[[], str] = field(default_factory=lambda: process_name)
    database_cls: Callable[..., Database] = field(default_factory=lambda: Database)
    pipeline_builder: Callable[..., Any] = field(default_factory=lambda: build_pipeline)


@contextmanager
def fresh_schedule_entry(
    *,
    hooks: ScheduleEntryHooks | None = None,
) -> Generator[ScheduleInputs]:
    import meiva

    active = hooks or ScheduleEntryHooks()
    image = active.image_loader()
    config = ConfigBoot()
    config.boot()
    machine = machine_from_boot(config)
    home = resolve_meiva_home()
    version = CodeVersion(image)
    gate = ProcessDbGate(process=active.process_name_fn(), version=version.get)

    def database() -> Database:
        return active.database_cls(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    clients = ClientSet(
        database=database,
        pipeline_factory=lambda: active.pipeline_builder(
            database=database, machine=machine, home=home
        ),
    )
    prior = getattr(meiva, "context", None)
    process_context.bind_context(
        MeivaContext(
            clients=clients,
            machine=machine,
            clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        )
    )
    try:
        yield ScheduleInputs(database, clients.event_pipeline, image, machine=machine)
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
