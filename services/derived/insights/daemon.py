# meiva:note
# The insights daemon: the process root that reads the configuration once into an `InsightsConfig`,
# builds the app from it and serves it on an owner-only unix socket. A changed `.env` reaches the
# daemon at its next start; the owned pool, socket and pidfile are released at exit.
# meiva:link services/derived/insights/docs/insights.meiva.okf.md
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from collections.abc import Callable
from pathlib import Path

import uvicorn

from base.agents.messages.timestamp_policy import TimestampSource
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.exit_codes import hard_exit
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


# meiva:note
# The process root reads these scalar service settings once. It constructs the timestamp
# source separately, alongside the database and catalog resources.
def config_from_boot(config: ConfigBoot) -> InsightsConfig:
    return InsightsConfig(
        run_timeline_message_text_max=config.view.display.run_timeline_message_text_max,
        default_model=config.view.lm.llm_model,
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
    config: InsightsConfig,
    catalog: ModelCatalog,
    database: Callable[[], Database],
    timestamps: TimestampSource,
) -> None:
    pidfile = insights_pidfile(home)
    if pidfile_holds_daemon(pidfile, _MODULE) or not acquire_pidfile(pidfile, _MODULE):
        _log.info("[insights] daemon already running (pidfile=%s), exiting", pidfile)
        sys.exit(1)
    path = insights_socket(home)
    db = database()
    pool = db.pool(max_size=_POOL_MAX_SIZE)
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                build_app(
                    db,
                    pool,
                    config,
                    home=home,
                    catalog=catalog,
                    timestamps=timestamps,
                    pid=os.getpid(),
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


def main() -> None:
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()

    config = ConfigBoot()
    config.boot()
    machine = ConfiguredMachineIdentity(config)
    process_home = meiva_home().resolve()
    assert_schema_current(config.view.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="insights")

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    pipeline = build_pipeline(database=database, machine=machine, home=process_home)
    init_gateway_process(
        name="insights",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
    )
    install_graceful_shutdown("insights")
    code = 0

    runner = asyncio.Runner()
    try:
        runner.run(
            run(
                home=meiva_home(),
                config=config_from_boot(config),
                catalog=build_model_catalog(),
                database=database,
                timestamps=TimestampSource(
                    clock=Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
                    timestamps_enabled=config.view.general.message_timestamps,
                ),
            )
        )
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
    hard_exit(code)


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
