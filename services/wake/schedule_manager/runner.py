# meiva:note
# A stale runner revision cannot execute a newer script, while current revision admission is recorded before successful completion.
# meiva:link services/wake/schedule_manager/docs/runner-composition.meiva.okf.md

from __future__ import annotations

import os
import random
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from loguru import logger

from base.agents.context import MeivaContext, deferred_event_bus
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.native_process.code_version import CodeVersion
from base.paths import meiva_home, prod_service_checkout_error
from base.telemetry import process_name
from meiva.boot import clients_over
from meiva.sdk_surface import process_context
from services.wake.schedule_manager import engine
from services.wake.schedule_manager.stall_policy import StallPolicy


def _bind_schedule_actor(
    schedule_id: int,
    *,
    clients: ClientSet,
    clock_factory: Callable[[], Clock],
    config: ConfigBoot,
    machine: MachineIdentityReader,
    home: Path,
    user_home: Path,
    rng: random.Random,
) -> None:
    process_context.bind_context(
        MeivaContext(
            identity=AgentIdentity(agent_id=None, owns_loop=True, actor=f"schedule:{schedule_id}"),
            clients=clients,
            machine=machine,
            clock_factory=clock_factory,
            bus_factory=deferred_event_bus(config, rng=rng, timers=SYSTEM_TIMERS),
            meiva_home=home,
            user_home=user_home,
            rng=rng,
        )
    )


def run(
    schedule_id: int, revision: int | None = None, *, config: ConfigBoot | None = None
) -> int:
    if config is None:
        config = ConfigBoot()
        config.read_process_environment()
    machine = ConfiguredMachineIdentity(config)
    image = process_context.loaded_code_image()
    version = CodeVersion(image)
    gate = ProcessDbGate(process=process_name(), version=version.get)
    home = meiva_home()

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=home),
        )

    def clock_factory() -> Clock:
        return Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS)

    clients = clients_over(config, machine, database)
    primary: BaseException | None = None
    try:
        rng = random.SystemRandom()
        # meiva:note
        # The script's `meiva.agents.*` calls attribute to the schedule: bind its actor before
        # the engine opens any history for it.
        _bind_schedule_actor(
            schedule_id,
            clients=clients,
            clock_factory=clock_factory,
            config=config,
            machine=machine,
            home=home,
            user_home=Path.home(),
            rng=rng,
        )
        return engine.run(
            schedule_id,
            revision,
            engine.RunnerRoot(
                database=database(),
                inputs=ScheduleInputs(
                    database, clients.event_pipeline, image, machine=machine, home=home
                ),
                plugins=engine.PluginInputs(config=config, machine=machine, clients=clients),
                policy=StallPolicy(timeout_s=config.view.gateway.schedule_stall_timeout_seconds),
                home=home,
                pid=os.getpid(),
                # meiva:note
                # This process entry records the thread the guard must watch once, before it
                # hands the runner to the engine.
                main_thread_id=threading.get_ident(),
            ),
        )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            clients.close(pipeline_timeout=2)
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"schedule clients cleanup failed: {cleanup!r}")


def main(argv: Sequence[str]) -> None:
    boot = ConfigBoot()
    boot.boot()
    if len(argv) not in (1, 2):
        logger.error(
            "Usage: python -m services.wake.schedule_manager.runner <schedule_id> [revision]"
        )
        raise SystemExit(2)

    refusal = prod_service_checkout_error(Path(__file__).resolve().parents[3])
    if refusal is not None:
        logger.error("schedule runner refused: {}", refusal)
        raise SystemExit(3)
    revision = int(argv[1]) if len(argv) == 2 else None
    if revision is not None and revision < 0:
        raise SystemExit(2)
    raise SystemExit(run(int(argv[0]), revision, config=boot))


if __name__ == "__main__":
    main(sys.argv[1:])
