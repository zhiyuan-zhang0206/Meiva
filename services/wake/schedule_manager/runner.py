# meiva:note
# A stale runner revision cannot execute a newer script, while current revision admission is recorded before successful completion.
# meiva:link services/wake/schedule_manager/docs/runner-composition.meiva.okf.md

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from loguru import logger

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import prod_service_checkout_error
from base.telemetry import process_name
from meiva.boot import process_clients
from meiva.sdk_surface import process_context
from services.wake.schedule_manager import engine


def _bind_schedule_actor(
    schedule_id: int,
    *,
    clients: ClientSet,
    clock_factory: Callable[[], Clock],
    machine: MachineIdentity,
    bind_context: Callable[[MeivaContext], None] = process_context.bind_context,
) -> None:
    bind_context(
        MeivaContext(
            identity=AgentIdentity(agent_id=None, owns_loop=True, actor=f"schedule:{schedule_id}"),
            clients=clients,
            machine=machine,
            clock_factory=clock_factory,
        )
    )


def _default_close_clients(clients: ClientSet, pipeline_timeout: float) -> None:
    clients.close(pipeline_timeout=pipeline_timeout)


@dataclass(frozen=True)
class RunnerDeps:
    loaded_code_image: Callable[[], LoadedCommit] = process_context.loaded_code_image
    code_version_cls: Callable[[LoadedCommit], CodeVersion] = CodeVersion
    database_cls: Callable[..., Database] = Database
    bind_context: Callable[[MeivaContext], None] = process_context.bind_context
    ensure_plugins_loaded: Callable[..., Any] = meiva.ensure_plugins_loaded
    engine_run: Callable[..., int] = engine.run
    close_clients: Callable[[ClientSet, float], None] = _default_close_clients


_DEFAULT_RUNNER_DEPS = RunnerDeps()


def run(
    schedule_id: int,
    revision: int | None = None,
    *,
    deps: RunnerDeps = _DEFAULT_RUNNER_DEPS,
) -> int:
    config = ConfigBoot()
    config.read_process_environment()
    machine = machine_from_boot(config)
    image = deps.loaded_code_image()
    version = deps.code_version_cls(image)
    gate = ProcessDbGate(process=process_name(), version=version.get)

    def database() -> Database:
        return deps.database_cls(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    def clock_factory() -> Clock:
        return Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS)

    clients = process_clients(config=config, database=database, machine=machine)
    primary: BaseException | None = None
    try:
        return deps.engine_run(
            schedule_id,
            revision,
            database=database,
            inputs=ScheduleInputs(database, clients.event_pipeline, image, machine=machine),
            bind_actor=partial(
                _bind_schedule_actor,
                clients=clients,
                clock_factory=clock_factory,
                machine=machine,
                bind_context=deps.bind_context,
            ),
            load_plugins=partial(
                deps.ensure_plugins_loaded,
                config=config,
                clock_factory=clock_factory,
                producer=clients.event_pipeline,
            ),
        )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            deps.close_clients(clients, 2)
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"schedule clients cleanup failed: {cleanup!r}")


def main(
    argv: Sequence[str] | None = None,
    *,
    checkout_error_fn: Callable[[Path], str | None] = prod_service_checkout_error,
) -> None:
    args = sys.argv if argv is None else argv
    if len(args) not in (2, 3):
        logger.error(
            "Usage: python -m services.wake.schedule_manager.runner <schedule_id> [revision]"
        )
        raise SystemExit(2)

    refusal = checkout_error_fn(Path(__file__).resolve().parents[3])
    if refusal is not None:
        logger.error("schedule runner refused: {}", refusal)
        raise SystemExit(3)
    revision = int(args[2]) if len(args) == 3 else None
    if revision is not None and revision < 0:
        raise SystemExit(2)
    raise SystemExit(run(int(args[1]), revision))


if __name__ == "__main__":
    main()
