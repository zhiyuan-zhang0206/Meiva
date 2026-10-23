# meiva:link services/derived/insights/docs/insights.meiva.okf.md
# meiva:note
# The daemon retains its supplied configuration owner: later model, timestamp and timezone edits are visible through that app's readers, while the owned pool and socket close at exit.
# meiva:link services/derived/insights/docs/insights.meiva.okf.md
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.exit_codes import hard_exit as _hard_exit
from base.lm.catalog import ModelCatalog
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.packages.plugin_providers import build_model_catalog
from base.paths import insights_pidfile, insights_socket, meiva_home
from base.telemetry import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.derived.insights.app import build_app
from services.derived.insights.config import InsightsConfig
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

_log = logging.getLogger("services.derived.insights.daemon")

_MODULE = "services.derived.insights.daemon"


_POOL_MAX_SIZE = 4


def insights_config(*, config: ConfigBoot) -> InsightsConfig:
    return InsightsConfig(
        run_timeline_message_text_max=config.view.display.run_timeline_message_text_max
    )


def bind_socket(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous = os.umask(0o177)
    try:
        sock.bind(str(path))
    except BaseException:
        sock.close()
        raise
    finally:
        os.umask(previous)
    return sock


async def run(
    *,
    home: Path,
    config: ConfigBoot,
    database: Callable[[], Database],
    catalog_factory: Callable[[], ModelCatalog] = build_model_catalog,
    server_cls: Callable[[uvicorn.Config], Any] = uvicorn.Server,
) -> None:
    pidfile = insights_pidfile(home)
    if pidfile_holds_daemon(pidfile, _MODULE) or not acquire_pidfile(pidfile, _MODULE):
        _log.info("[insights] daemon already running (pidfile=%s), exiting", pidfile)
        sys.exit(1)
    path = insights_socket(home)
    db = database()
    pool = db.pool(max_size=_POOL_MAX_SIZE)
    try:
        server = server_cls(
            uvicorn.Config(
                build_app(
                    db,
                    pool,
                    insights_config(config=config),
                    home=home,
                    catalog=catalog_factory(),
                    default_model_reader=lambda: config.view.lm.llm_model,
                    timeline_inputs=TimelineReadInputs(
                        clock_factory=lambda: Clock(
                            clock_config_from_boot(config), timers=SYSTEM_TIMERS
                        ),
                        timestamps_enabled=lambda: config.view.general.message_timestamps,
                    ),
                ),
                log_level="warning",
                access_log=False,
                log_config=None,
            )
        )
        sock = bind_socket(path)
        _log.info("[insights] serving on %s", path)
        await server.serve(sockets=[sock])
    finally:
        pool.close()
        path.unlink(missing_ok=True)
        remove_pidfile(pidfile)
        _log.info("[insights] daemon stopped")


@dataclass(frozen=True)
class MainDeps:
    capture_image: Callable[[], LoadedCommit] = LoadedCommit.capture
    config_boot_cls: Callable[[], ConfigBoot] = ConfigBoot
    check_schema: Callable[[str], None] | None = None
    code_version_cls: Callable[[LoadedCommit], CodeVersion] = CodeVersion
    database_cls: Callable[..., Database] = Database
    build_pipeline: Callable[..., Any] = build_pipeline
    init_gateway_process: Callable[..., None] = init_gateway_process
    install_graceful_shutdown: Callable[[str], None] = install_graceful_shutdown
    runner_factory: Callable[[], asyncio.Runner] = asyncio.Runner
    run_fn: Callable[..., Coroutine[Any, Any, None]] | None = None


def main(
    *,
    exit_process: Callable[[int], None] = _hard_exit,
    deps: MainDeps = MainDeps(),
) -> None:
    from base.deploy.schema.migrations import assert_schema_current

    image = deps.capture_image()

    config = deps.config_boot_cls()
    config.boot()
    machine = machine_from_boot(config)
    process_home = meiva_home().resolve()
    active_check_schema = assert_schema_current if deps.check_schema is None else deps.check_schema
    active_check_schema(config.view.data_plane.db_url)
    version = deps.code_version_cls(image)
    gate = ProcessDbGate(version=version.get, process="insights")

    def database() -> Database:
        return deps.database_cls(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    pipeline = deps.build_pipeline(database=database, machine=machine, home=process_home)
    deps.init_gateway_process(
        name="insights",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
    )
    deps.install_graceful_shutdown("insights")
    code = 0

    active_run = run if deps.run_fn is None else deps.run_fn
    runner = deps.runner_factory()
    try:
        runner.run(active_run(home=meiva_home(), config=config, database=database))
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _log.info("[insights] interrupted, shutting down")
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[insights] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[insights] fatal error, shutting down")
        code = 1
    exit_process(code)


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
