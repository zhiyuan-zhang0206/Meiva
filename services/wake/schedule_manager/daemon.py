"""Schedule manager daemon — keeps one resident session alive per enabled schedule.

Two resident sequential loops under one `TaskGroup` (`run_loops`):

- `reconcile` (`ScheduleManager.reconcile`): every few seconds, desired (enabled
  `schedules` rows) against actual (live `meiva-schedule-<id>` sessions) — launch
  the missing ones once per explicit revision, reap the unwanted, alert on a
  schedule sessionless for two hours.
- `requests` (`services.wake.schedule_manager.requests`): every second, converge the
  schedules the API asked for (start / stop / restart / edit / delete).

The launch latch and the stall-alert clock are columns of the schedule row, so a
restart of this service resumes them. A loop that raises
cancels its sibling and the exception leaves `run`: the process exits and the
supervisor restarts it; the schedule sessions themselves survive the service.
An unreachable database skips a round. Each loop reports its own progress to
`/healthz`.

Runs on the gateway, one per cluster, from the home's own checkout: a checkout
that does not own the home refuses to supervise schedules, because the sessions
run that checkout's code (issue #194). Kept alive by the root supervisor's health
monitor through the roster's `/healthz` identity probe (`services/supervision/roster/healthz.py`).

Usage:
    .venv/bin/python -m services.wake.schedule_manager.daemon
"""

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeGuard

from psycopg_pool import ConnectionPool

from base.clock import SYSTEM_TIMERS
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import (
    GatewayAccess,
    GatewayApiTokenMissing,
    gateway_bearer,
)
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.daemon import round_loop
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.loop_health import LivenessGroup
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.env.dotenv_file import read_env_aliases
from base.host.exit_codes import hard_exit as _hard_exit
from base.host.net.auth import bearer_header
from base.host.net.http_dial import get as dial_get
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home, prod_service_checkout_error
from base.sessions.backend import get_shell_backend
from base.sessions.env_forwarding import forward_env_dict
from base.telemetry.pipeline_build import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.wake.schedule_manager import requests
from services.wake.schedule_manager.manager import POLL_INTERVAL_S, REPO_ROOT, ScheduleManager
from services.wake.schedule_manager.manager_policy import ManagerPolicy

_log = logging.getLogger("services.wake.schedule_manager.daemon")

# One reconcile can reap several sessions and launch several runners, each a
# bounded backend call; a loop that has finished no round for this long is wedged.
_LIVENESS_TIMEOUT_S = 300.0
_REQUESTS_INTERVAL_S = 1.0
# The two loops' concurrent statements, each in its own worker thread.
_POOL_MAX_SIZE = 4


def _endpoint(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
    runtime.mkdir(parents=True, exist_ok=True)
    return ServiceEndpoints.from_boot(config, runtime=runtime).of("schedule_manager")


def _pidfile(config: ConfigBoot, *, runtime: Path) -> Path:
    return _endpoint(config, runtime=runtime).pidfile


def _is_running(config: ConfigBoot, *, runtime: Path) -> bool:
    """Whether a daemon is already running (via its pidfile); pid-reuse-safe."""
    return pidfile_holds_daemon(
        _pidfile(config, runtime=runtime), "services.wake.schedule_manager.daemon"
    )


def manager_policy(config: ConfigBoot) -> ManagerPolicy:
    gateway = config.view.gateway
    return ManagerPolicy(
        auto_restart_max_per_window=gateway.schedule_auto_restart_max_per_window,
        auto_restart_window_seconds=gateway.schedule_auto_restart_window_seconds,
        recovery_gate_alert_seconds=gateway.schedule_recovery_gate_alert_seconds,
        timezone=config.view.general.timezone,
    )


async def run_loops(
    pool: ConnectionPool, manager: ScheduleManager, liveness: LivenessGroup
) -> None:
    """Own the two resident loops; one that raises ends the process."""
    reconcile_progress = liveness.register("reconcile", _LIVENESS_TIMEOUT_S)
    requests_progress = liveness.register("requests", _LIVENESS_TIMEOUT_S)

    async def reconcile_round() -> None:
        await asyncio.to_thread(lambda: manager.reconcile(now=time.monotonic()))

    async def requests_round() -> None:
        await asyncio.to_thread(
            lambda: requests.consume_requests(pool, manager, now=time.monotonic())
        )

    async with asyncio.TaskGroup() as loops:
        loops.create_task(
            round_loop.run_rounds("reconcile", reconcile_progress, POLL_INTERVAL_S, reconcile_round)
        )
        loops.create_task(
            round_loop.run_rounds(
                "requests", requests_progress, _REQUESTS_INTERVAL_S, requests_round
            )
        )


# meiva:note
# Start the daemon: checkout guard -> pidfile -> healthz -> DB -> loops.
#
# ``runtime`` is the home's run directory, the signature the resident services share; the
# home the manager launches schedule sessions under is its parent.
def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _fetch_gateway_launch_secrets(
    access: GatewayAccess,
    schedule_id: int,
    missing_names: list[str],
) -> dict[str, str]:
    if not missing_names or not access.gateway_url:
        return {}
    try:
        bearer = gateway_bearer(access)
    except GatewayApiTokenMissing:
        return {}
    except Exception as exc:
        _log.warning("failed generating gateway bearer token: %s", exc, exc_info=True)
        return {}

    url = f"{access.gateway_url.rstrip('/')}/api/schedules/{schedule_id}/launch-env"
    result: dict[str, str] = {}
    try:
        resp = dial_get(url, timeout=5.0, headers=bearer_header(bearer))
        if resp.status_code == 200:
            remote_secrets: object = resp.json()
            if _is_dict(remote_secrets):
                for k in missing_names:
                    remote_val = remote_secrets.get(k)
                    if isinstance(remote_val, str):
                        result[k] = remote_val
    except Exception as exc:
        _log.warning(
            "failed fetching launch secrets from gateway for schedule %s: %s",
            schedule_id,
            exc,
            exc_info=True,
        )
    return result


def _resolve_local_secrets(
    runtime: Path,
    process_config: ConfigBoot,
    secret_names: tuple[str, ...],
) -> dict[str, str]:
    dotenv_aliases: Mapping[str, str] = {}
    try:
        dotenv_aliases = read_env_aliases(runtime.parent / ".env")
    except OSError:
        dotenv_aliases = {}
    local_env = process_config.process_environment()

    result: dict[str, str] = {}
    for k in secret_names:
        if k.startswith("MEIVA_"):
            continue
        val = dotenv_aliases.get(k)
        if val is None:
            val = local_env.get(k)
        if val is not None:
            result[k] = val
    return result


def _build_secret_env_resolver(
    process_config: ConfigBoot, runtime: Path
) -> Callable[[int, tuple[str, ...]], Mapping[str, str]]:
    access = gateway_access_from_boot(process_config)

    def _resolve(schedule_id: int, secret_names: tuple[str, ...]) -> Mapping[str, str]:
        result = _resolve_local_secrets(runtime, process_config, secret_names)
        missing = [k for k in secret_names if k not in result and not k.startswith("MEIVA_")]
        if missing and access.gateway_url:
            remote = _fetch_gateway_launch_secrets(access, schedule_id, missing)
            result.update(remote)
        return result

    return _resolve


async def run(
    *,
    process_config: ConfigBoot,
    runtime: Path,
    database: Callable[[], Database],
    image: LoadedCommit,
) -> None:
    refusal = prod_service_checkout_error(REPO_ROOT)
    if refusal is not None:
        raise RuntimeError(f"schedule supervision refused: {refusal}")
    if _is_running(process_config, runtime=runtime) or not acquire_pidfile(
        _pidfile(process_config, runtime=runtime),
        "services.wake.schedule_manager.daemon",
        pid=os.getpid(),
    ):
        _log.info(
            "[schedule-manager] daemon already running (pidfile=%s), exiting",
            _pidfile(process_config, runtime=runtime),
        )
        sys.exit(1)
    _log.info("[schedule-manager] pidfile written: %s", _pidfile(process_config, runtime=runtime))

    liveness = LivenessGroup(timers=SYSTEM_TIMERS)
    endpoint = _endpoint(process_config, runtime=runtime)
    health = await start_health_server(
        "schedule_manager", endpoint.health_port, home=meiva_home(), liveness=liveness, image=image
    )
    _log.info("[schedule-manager] healthz listening on :%s", endpoint.health_port)

    pool = database().pool(max_size=_POOL_MAX_SIZE)
    try:
        machine = ConfiguredMachineIdentity(process_config)
        manager = ScheduleManager(
            pool,
            backend=get_shell_backend(home=runtime.parent),
            repo_root=REPO_ROOT,
            home=runtime.parent,
            policy=manager_policy(process_config),
            launch_env=forward_env_dict(process_config.process_environment()),
            machine_name=machine.name(),
            secret_env_resolver=_build_secret_env_resolver(process_config, runtime),
        )
        await run_loops(pool, manager, liveness)
    finally:
        pool.close()
        await stop_health_server(health)
        remove_pidfile(_pidfile(process_config, runtime=runtime))
        _log.info("[schedule-manager] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop."""
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()
    machine_config = ConfigBoot()
    machine_config.boot()
    machine = ConfiguredMachineIdentity(machine_config)
    process_home = meiva_home().resolve()
    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(machine_config.view.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="schedule_manager")

    def database() -> Database:
        return Database(
            db_config_from_boot(machine_config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=process_home),
        )

    pipeline = build_pipeline(database=database, machine=machine, home=process_home)
    init_gateway_process(
        name="schedule_manager",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
        home=process_home,
    )
    install_graceful_shutdown("schedule_manager")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — and a stop signal must never wait on those (see `_hard_exit`).
    runner = asyncio.Runner()
    try:
        runner.run(
            run(
                process_config=machine_config,
                runtime=process_home / "run",
                database=database,
                image=image,
            )
        )
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[schedule-manager] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loops'
        # tasks explicitly: `run`'s finally still stops the health server,
        # closes the DB pool and removes the pidfile.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[schedule-manager] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("[schedule-manager] fatal error, shutting down")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
