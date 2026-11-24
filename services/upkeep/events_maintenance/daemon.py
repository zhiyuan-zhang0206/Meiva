# meiva:note
# Gateway-owned events-maintenance daemon. Resident loops under one `TaskGroup`:
# - Hourly dispatch (`telemetry_replay`, `blob_vacuum`, checkpoint size sample)
# - 60s `registry_gauge` (`max(agents.id)` -> `agent_registry`)
# - 5m Grafana `alert_reconciliation` (when credentialed)
# Each loop tracks independent progress and hard deadlines for `/healthz`; exceeding a deadline
# fails healthz until the watchdog restarts the process.
# meiva:link services/docs/gateway_side/events_maintenance/events_maintenance.md

import asyncio
import os
import random
import signal
import socket
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from psycopg_pool import ConnectionPool

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import (
    LivenessGroup,
    LoopProgress,
    start_health_server,
    stop_health_server,
)
from base.daemon.health_schema import component
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.events.live.bus import EventBus, event_bus_config_from_config
from base.host.exit_codes import hard_exit
from base.host.exit_codes import hard_exit as _hard_exit
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from base.telemetry.pipeline_build import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from services.upkeep.events_maintenance import (
    alert_reconciler,
    audit_identity,
    health_components,
    loop_roster,
    passes,
    registry_gauge,
)
from services.upkeep.events_maintenance.blob_vacuum import (
    emit_checkpoint_table_sizes,
    run_blob_vacuum,
)
from services.upkeep.events_maintenance.config import EventsMaintenanceConfig
from services.upkeep.events_maintenance.health_components import LoopFacts
from services.upkeep.events_maintenance.passes import events_maintenance_log
from services.upkeep.events_maintenance.telemetry_replay import recover_telemetry_events


# meiva:note
# The process's random generator, built once at its entry for the event bus's retry jitter. Jitter
# is not a secret, so it is a plain `random.Random`.
def _new_rng() -> random.Random:
    return random.Random()  # noqa: S311


# meiva:note
# The service's configuration, read once from the process's `ConfigBoot`. `mirror_dir` is where the
# telemetry JSONL mirror lives; the entry knows its home, so the replay does not look it up.
def events_maintenance_config(config: ConfigBoot, *, mirror_dir: Path) -> EventsMaintenanceConfig:
    return EventsMaintenanceConfig(
        events_maintenance_interval_seconds=config.view.daemon.events_maintenance_interval_seconds,
        events_maintenance_pass_deadline_s=config.view.daemon.events_maintenance_pass_deadline_s,
        timezone=config.view.general.timezone,
        grafana_host=config.view.gateway.grafana_host,
        grafana_port=config.view.gateway.grafana_port,
        grafana_admin_password=config.view.alerts.grafana_admin_password,
        mirror_dir=mirror_dir,
    )


def events_maintenance_endpoint(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
    runtime.mkdir(parents=True, exist_ok=True)
    return ServiceEndpoints.from_boot(config, runtime=runtime).of("events_maintenance")


def _pidfile(config: ConfigBoot, *, runtime: Path) -> Path:
    return events_maintenance_endpoint(config, runtime=runtime).pidfile


# meiva:note
# One hourly pass: the telemetry mirror replay, the unknown-writer audit, the hourly checkpoint
# size/row-count telemetry sample, and the blob VACUUM. One `now` drives the time-based steps. Logs
# what each step did; a no-op pass logs nothing.
def run_maintenance(
    pool: ConnectionPool,
    progress: LoopProgress,
    config: EventsMaintenanceConfig,
    db: Database,
    *,
    now: datetime,
    host: str,
) -> None:
    # The telemetry_events record is independent of the other steps: its writer reports its own
    # failures and the mirror holds what it missed, so a failing replay must not prevent the
    # remaining maintenance work from making progress.
    try:
        with pool.connection() as conn:
            replayed = recover_telemetry_events(conn, logs=config.mirror_dir, host=host)
        if replayed:
            events_maintenance_log.info(
                "[events-maintenance] replayed %d telemetry events from the mirror", replayed
            )
    except Exception:
        events_maintenance_log.exception("[events-maintenance] telemetry events replay incomplete")
    progress.beat()
    # meiva:note
    # A row whose writer could not name its machine is the shape `#6089` made readable and nothing
    # read. The window spans one interval plus the pass deadline, so a slow pass cannot open a gap
    # between two counts and let a row through unreported (task #6235).
    audit_identity.report_unknown_writers(
        pool,
        window_seconds=config.events_maintenance_interval_seconds
        + config.events_maintenance_pass_deadline_s,
        now=now,
    )
    progress.beat()
    # Hourly checkpoint size/row-count sample: the gauge is also emitted after
    # each blob vacuum, but the vacuum only runs inside the 05:00-08:00
    # cluster-time window — emitting here on every pass keeps the series dense
    # enough for growth-curve and rate queries the rest of the day.
    with pool.connection() as conn:
        emit_checkpoint_table_sizes(conn)
    progress.beat()
    # Incremental physical reclamation: a plain VACUUM (no lock) over the
    # checkpoint tables, only inside the measured agent-lowest window
    # (05:00-08:00 CLUSTER time). Logs size + dead tuples each run.
    vacuum_result = run_blob_vacuum(db, timezone=config.timezone, now=now)
    progress.beat()
    if vacuum_result.ran:
        events_maintenance_log.info("[events-maintenance] blob vacuum: %s", vacuum_result.summary())
    progress.mark_success()


def _clock(config: EventsMaintenanceConfig) -> Clock:
    return Clock(
        ClockConfig(
            timezone=config.timezone,
            authoritative_timezone=config.timezone,
            message_timestamp_weekday=False,
        ),
        timers=SYSTEM_TIMERS,
    )


def write_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    if not acquire_pidfile(
        _pidfile(config, runtime=runtime),
        "services.upkeep.events_maintenance.daemon",
        pid=os.getpid(),
    ):
        events_maintenance_log.info(
            "[events_maintenance] daemon already running (pidfile=%s), exiting",
            _pidfile(config, runtime=runtime),
        )
        sys.exit(1)


def _remove_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    remove_pidfile(_pidfile(config, runtime=runtime))


def _is_running(config: ConfigBoot, *, runtime: Path) -> bool:
    """Whether a daemon is already running (via its pidfile).

    Pid-reuse-safe: a live pid whose argv does not name this daemon's module
    is a recycled pid, not a running instance (audit round 2, P1)."""
    return pidfile_holds_daemon(
        _pidfile(config, runtime=runtime), "services.upkeep.events_maintenance.daemon"
    )


# meiva:note
# Project each registered loop tracker into the shared health envelope. `timeouts` is the deadline
# each loop was registered with, which the tracker's snapshot does not carry.
def _snapshot_error(value: object) -> str | None:
    match value:
        case {"message": str(message)}:
            return message
        case _:
            return None


def loop_components(
    liveness: LivenessGroup, timeouts: Mapping[str, float], *, now: float
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for name, snapshot in liveness.snapshot().items():
        stale_for = snapshot["stale_for"]
        wedged = snapshot["wedged"]
        last_success_at = snapshot["last_success_at"]
        facts = health_components.component_facts(
            LoopFacts(
                name=name,
                timeout_s=timeouts[name],
                stale_for_s=float(stale_for) if isinstance(stale_for, int | float) else 0.0,
                wedged=bool(wedged),
                last_success_at=last_success_at if isinstance(last_success_at, str) else None,
                last_error=_snapshot_error(snapshot["last_error"]),
            )
        )
        records.append(
            component(
                facts.name,
                facts.status,
                last_success=facts.last_success,
                last_error=facts.last_error,
                progress=facts.progress,
                detail=facts.detail,
                now=now,
            )
        )
    return records


# meiva:note
# Main loop: run immediately on start (fresh after a restart), then every
# interval. The maintenance DB work is synchronous psycopg run in a thread so it does
# not block the healthz event loop.
async def dispatch_loop(
    pool: ConnectionPool,
    progress: LoopProgress,
    config: EventsMaintenanceConfig,
    db: Database,
    *,
    tasks: asyncio.TaskGroup,
) -> None:
    interval = config.events_maintenance_interval_seconds
    events_maintenance_log.info(
        "[events-maintenance] daemon started, interval=%.0fs",
        interval,
    )
    host = socket.gethostname()
    await passes.pass_loop(
        progress,
        label="maintenance",
        interval_s=interval,
        work=lambda: run_maintenance(
            pool,
            progress,
            config,
            db,
            now=_clock(config).now(),
            host=host,
        ),
        tasks=tasks,
    )


async def run(
    *,
    process_config: ConfigBoot,
    runtime: Path,
    database: Callable[[], Database],
    image: LoadedCommit,
) -> None:
    """Start healthz, register per-loop progress, then enter all resident loops."""
    if _is_running(process_config, runtime=runtime):
        events_maintenance_log.info(
            "[events-maintenance] daemon already running (pidfile=%s), exiting",
            _pidfile(process_config, runtime=runtime),
        )
        sys.exit(1)

    # Publish the pidfile before binding healthz so identity-aware probes can verify it.
    write_pidfile(process_config, runtime=runtime)
    events_maintenance_log.info(
        "[events-maintenance] pidfile written: %s", _pidfile(process_config, runtime=runtime)
    )

    # meiva:note
    # The telemetry mirror lives in the home's logs directory, beside the runtime directory.
    config = events_maintenance_config(process_config, mirror_dir=runtime.parent / "logs")
    # meiva:note
    # The health component times and the alert reconciler's snapshot stamps share this one instance,
    # so a single clock decides both and a test that steers it steers them together. The dispatch and
    # resolution loops still build their own from the same configuration.
    clock = _clock(config)
    # Only a unit holding the Grafana credential reconciles alerts; one without it
    # carries no such loop (and no tracker to read as idle).
    loops = loop_roster.roster(
        dispatch_deadline_s=config.events_maintenance_pass_deadline_s,
        reconcile_alerts=alert_reconciler.grafana_reconciliation_configured(config),
    )
    liveness = LivenessGroup(clock=clock)
    progress = {spec.name: liveness.register(spec.name, spec.deadline_s) for spec in loops}
    timeouts = {spec.name: spec.deadline_s for spec in loops}
    endpoint = events_maintenance_endpoint(process_config, runtime=runtime)
    health = await start_health_server(
        "events_maintenance",
        endpoint.health_port,
        home=meiva_home(),
        liveness=liveness,
        components=lambda: loop_components(liveness, timeouts, now=clock.now().timestamp()),
        image=image,
    )
    events_maintenance_log.info(
        "[events-maintenance] healthz listening on :%s", endpoint.health_port
    )

    db = database()
    pool = db.pool()
    try:
        # The service TaskGroup owns resident loops and their in-flight passes.
        # An expired pass remains owned while healthz asks the watchdog to replace
        # the process. Stop cancels its async proxy; main's hard exit never joins
        # the blocking executor thread.
        async with asyncio.TaskGroup() as group:
            group.create_task(dispatch_loop(pool, progress["dispatch"], config, db, tasks=group))
            group.create_task(registry_gauge.registry_gauge_loop(pool, progress["registry_gauge"]))
            if "alert_reconciliation" in progress:
                bus = EventBus(
                    event_bus_config_from_config(process_config),
                    rng=_new_rng(),
                    timers=SYSTEM_TIMERS,
                )
                group.create_task(
                    alert_reconciler.reconciliation_loop(
                        pool, bus, progress["alert_reconciliation"], config, clock=clock
                    )
                )
    finally:
        pool.close()
        await stop_health_server(health)
        _remove_pidfile(process_config, runtime=runtime)
        events_maintenance_log.info("[events-maintenance] daemon stopped")


def main() -> None:
    """Entry point: init logger + run asyncio loop.

    SIGTERM (the graceful stop the fleet update sends) and Ctrl-C converge on
    the same `KeyboardInterrupt` unwind — see `base.daemon.shutdown`. `meiva stop`
    default force-kill does not reach this.
    """
    from base.deploy.schema.migrations import assert_schema_current

    image = LoadedCommit.capture()
    machine_config = ConfigBoot()
    machine_config.boot()
    machine = ConfiguredMachineIdentity(machine_config)
    process_home = meiva_home().resolve()
    # Pre-startup sanity: schema version must match code; raises SchemaVersionMismatch if not.
    assert_schema_current(machine_config.view.data_plane.db_url)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="events_maintenance", end_process=hard_exit)

    def database() -> Database:
        return Database(
            db_config_from_boot(machine_config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=process_home),
        )

    pipeline = build_pipeline(
        database=database,
        machine=machine,
        home=process_home,
        now=Clock(clock_config_from_boot(machine_config), timers=SYSTEM_TIMERS).now,
        otlp_enabled=lambda: machine_config.view.observability.telemetry_otlp_enabled,
        otlp_endpoint=lambda: machine_config.view.observability.telemetry_otlp_endpoint,
        rng=_new_rng(),
    )
    init_gateway_process(
        name="events_maintenance",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
        home=process_home,
    )
    install_graceful_shutdown("events_maintenance")
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — an in-flight maintenance pass among them — and a stop signal
    # must never wait on those (see `_hard_exit`). The runner is therefore
    # never closed: after the explicit drain below, teardown is skipped by the
    # hard exit.
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
        events_maintenance_log.info("[events-maintenance] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: run()'s finally still closes the pool, stops the
        # health server and removes the pidfile. The executor is deliberately
        # NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            events_maintenance_log.error("[events-maintenance] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        events_maintenance_log.exception(
            "[events-maintenance] daemon crashed — uncaught exception escaped run()"
        )
        code = 1
    finally:
        _remove_pidfile(machine_config, runtime=process_home / "run")
    _hard_exit(code)


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
