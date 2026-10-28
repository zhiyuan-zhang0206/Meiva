# meiva:note
# Agent-host daemon — the supervised process that runs every local agent's turns.
#
# The service roster owns wake dispatch, turns, health, boot and shutdown through this process.
#
# Usage:
#     .venv/bin/python -m services.agent_runner.agent_host.daemon
#
# ## Boot order, and why it is this one
#
# 1. **Instance lock and pidfile**, so a second instance cannot race the first for
#    turns. Independent exec clients can still own envelopes in `<home>/run/exec`;
#    the host does not delete their shared directory at startup.
# 2. **Process-scope boot** — trace export precedes models and skills materialize
#    once. Plugins load once (issue #170); new extensions require a host restart.
# 3. **The shared data plane** — workload/control pools, checkpointer and graph.
#    Recover predecessor forces only with durable resource-free exec evidence.
# 4. **Healthz**, after boot, so a green probe means the host can take a turn.
# 5. **The dispatcher**, after serving is ready, so subscribed wakes are not lost.
#
# Shutdown joins dispatcher siblings and drains turns before collecting the
# installation's sampling refresh. Health and ownership callbacks still unwind;
# joined host pools close even if sampling reports an original failure.
# The host has no agent identity: process logs remain unattributed, agent events
# carry explicit identity, and the SDK entry rejects inherited child identity
# before boot work.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import secrets
import signal
import sys
from collections.abc import Awaitable, Callable, Collection, Coroutine, Iterable, Mapping
from functools import partial
from pathlib import Path

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.log import logger, prepare_process_logging

# The executable owns inherited SDK posture; importing this module never boots it.
if __name__ == "__main__":
    prepare_process_logging()
    from base.native_process.child_env import inherited_process_env

    if "MEIVA_AGENT_ID" in inherited_process_env():
        raise RuntimeError("a shared agent host cannot inherit a launched-agent identity")
    from meiva.sdk_surface import process_context as _posture

    _posture.bind_host_process()

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from agent.graph.exec.process import ParentFacts
from agent.llm import execute_code
from agent.ownership.hosted import settle_running_rows_at_start
from base.agents.context.clients import ClientSet
from base.clock.ids import new_uuid
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import Liveness, start_health_server, stop_health_server
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.deploy.release import editable_install
from base.deploy.timing import assert_clock_lattice, clock_values
from base.events.live.bus import EventBus, EventBusConfig
from base.host.exit_codes import HELPER_CHAIN_BROKEN_EXIT_CODE
from base.host.exit_codes import hard_exit as _hard_exit
from base.host.runtime_interpreter import external_plugin_read_root
from base.lm.harness.registration import SecureRandom
from base.native_process.code_version import CodeVersion
from base.native_process.stop_timing import CANCEL_UNWIND_TIMEOUT_S
from base.packages.composer_commands import CommandCatalog
from base.paths import logs_dir
from base.sessions.helper_chain_guard import helper_marker_pid, parent_chain_intact
from meiva import skills as meiva_skills
from meiva.boot import clients_over
from meiva.sdk_surface import process_context
from meiva.sdk_surface.installation import Installation
from services.agent_runner.understanding.chunk_consumer import understanding_loop_forever

from ...pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile
from .dispatcher import InboundWakeDispatcher, TurnScheduler
from .exec_memory_guard import run_memory_guard_forever
from .health_routes import cancel_turn_route, stats_route
from .host import AgentHost
from .host_beat import close_ownership_beat, ownership_beat_completion
from .lifecycle.configuration import (
    boot_process,
    handoff_source,
    host_policy,
    initialize_logging,
    load_installation,
    pool_bounds,
    relief_floor_bytes,
)
from .lifecycle.graceful_exit import OwedRestart, serve_until_exit, write_exit_restart
from .lifecycle.instance import release_instance_lock, take_instance_lock
from .pooled_checkpoint import PooledPostgresSaver
from .pools import build_control_pool, build_shared_pool
from .pools import close_host_pools as close_host_pools
from .stdout_log import rotate_stdout_log_forever, stdout_log_path

host_log = logging.getLogger("services.agent_runner.agent_host.daemon")

_MODULE = "services.agent_runner.agent_host.daemon"


def _endpoint() -> ServiceEndpoint:
    return ServiceEndpoints.from_settings().of("agent_host")


def pidfile_path() -> Path:
    return _endpoint().pidfile


_pidfile = pidfile_path


def _claim_pidfile() -> None:
    if is_running():
        host_log.info("[agent-host] daemon already running (pidfile=%s), exiting", _pidfile())
        sys.exit(1)
    if not acquire_pidfile(_pidfile(), _MODULE):
        host_log.info("[agent-host] could not acquire pidfile %s, exiting", _pidfile())
        sys.exit(1)


# meiva:note
# The liveness probe window the beat refreshes; the beat itself lives in host_beat.py.
_LIVENESS_TIMEOUT_S = 60.0


# Plugin-discovery watchdog (issue #170): the host loads external plugins
# exactly once per process (`meiva.sdk_surface.extensions.load_extensions`), so a plugin installed
# after boot is invisible to every agent on this runner until a restart. The
# runner's supervisor (watchdog -> healthcheck) restarts a dead host within a
# minute, so the fix is not a reload (plugin-spec-v2's S4 dispose contract is
# unimplemented — a second load would leak and fork class identity) but an
# intentional, ergonomic restart: watch $MEIVA_HOME/plugins, and on any change
# exit so the supervisor brings the host back with the new plugin loaded.
PLUGINS_POLL_INTERVAL_S = 30.0


def plugins_fingerprint(root: Path) -> str:
    """A cheap fingerprint of the external-plugin directory.

    One entry per plugin subdirectory: name + plugin.py (size, mtime_ns).
    Changing, adding, or removing a plugin changes the fingerprint; touching
    any other file under the dir does not. The directory itself missing is a
    valid state (no plugins) — the fingerprint is then empty, not an error.
    """
    if not root.exists():
        return ""
    parts: list[str] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        plugin_py = sub / "plugin.py"
        if not plugin_py.exists():
            continue
        st = plugin_py.stat()
        parts.append(f"{sub.name}:{st.st_size}:{st.st_mtime_ns}")
    return "|".join(parts)


# meiva:note
# Exit the host when the plugin directory changes under it. Runs for the daemon's whole
# life; on a fingerprint change it logs the names that changed and raises SIGTERM at
# itself, which starts the graceful exit: running turns settle at their restart, the
# drains run, then the supervisor restarts the host fresh.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
async def watch_plugins_for_restart(root: Path, *, interval_s: float) -> None:
    baseline = plugins_fingerprint(root)
    while True:
        await asyncio.sleep(interval_s)
        now = plugins_fingerprint(root)
        if now == baseline:
            continue
        host_log.info(
            "[agent-host] external plugins changed under $MEIVA_HOME/plugins — "
            "restarting to load them (issue #170)"
        )
        signal.raise_signal(signal.SIGTERM)
        return


async def _close_host_runtime(
    host: AgentHost,
    scheduler: TurnScheduler,
    beat: asyncio.Task[Exception | None] | None,
    beat_tasks: asyncio.TaskGroup,
) -> None:
    """Drain turns and release settled ownership after background loops join.
    Reverse callbacks attempt every cleanup before failure propagates; pools
    must outlive active turns.
    """
    # Share the existing unwind allowance; late resource join must not add a
    # second five-second window after the scheduler has spent its own budget.
    resource_deadline = asyncio.get_running_loop().time() + CANCEL_UNWIND_TIMEOUT_S
    async with contextlib.AsyncExitStack() as cleanup:
        cleanup.push_async_callback(host.aclose, resource_deadline=resource_deadline)
        cleanup.push_async_callback(close_ownership_beat, beat, beat_tasks)
        cleanup.push_async_callback(scheduler.aclose)


def background_loops(
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    db: Database,
    *,
    installation: Installation,
    config: ConfigBoot,
    plugins_root: Path,
    stdout_log: Path,
) -> dict[str, Coroutine[object, object, None]]:
    """The daemon's background loops for plugins, logs, exec memory and understanding chunks.

    Split out of `run()` so the wiring is testable without booting the
    dispatcher: the rotator's existence is what keeps a traceback storm from
    filling the disk through the uncapped raw transcript (task #2356), and a
    regression that dropped it must turn a test red rather than silently reopen
    the gap. `run` starts them in one `TaskGroup`.
    """
    return {
        "plugins_watch": watch_plugins_for_restart(
            plugins_root, interval_s=PLUGINS_POLL_INTERVAL_S
        ),
        "stdout_log_rotate": rotate_stdout_log_forever(stdout_log),
        "exec_memory_guard": run_memory_guard_forever(host_log, os.getpid(), relief_floor_bytes()),
        "understanding_chunks": understanding_loop_forever(
            control_pool,
            db,
            [execute_code],
            catalog=installation.require_catalog(),
            keys=installation.require_provider_keys(),
            llm_override=config.view.lm.llm_override,
            config=config,
        ),
    }


async def build_checkpointer(
    pool: AsyncConnectionPool[psycopg.AsyncConnection],
    plugin_state_classes: Iterable[type[BaseModel]] = (),
    *,
    checkpoint_interval: int,
) -> AsyncPostgresSaver:
    """One saver for the whole host, over the workload pool.

    `plugin_state_classes` are the BaseModels the loaded plugins declare as state: the checkpoint
    serde must be allowed to decode them.

    No `setup()` call: the runner role holds no CREATE on the schema by design
    (task #1236), and the gateway owns langgraph's own migrations. A host booting
    against a schema that lacks the checkpoint tables fails on first use, loudly,
    which is the correct outcome for a runner that should never have been
    pointed at an unmigrated database.
    """
    from agent.startup import (
        wrap_saver_writes_with_loud_failure,
        wrap_saver_writes_with_nstep_interval,
    )
    from agent.state import build_checkpoint_serde
    from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction

    checkpointer = PooledPostgresSaver.from_pool(
        pool, serde=build_checkpoint_serde(plugin_state_classes)
    )
    wrap_saver_writes_with_loud_failure(checkpointer)
    wrap_saver_writes_with_nstep_interval(checkpointer, checkpoint_interval)
    # Transition layer (tasks #3180/#3181): vanilla-era readers must see
    # delta-written threads' messages. Inert on vanilla-written data.
    wrap_saver_reads_with_delta_reconstruction(checkpointer)
    return checkpointer


async def open_host_pools(
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
) -> None:
    """Open both client pools before the scheduler can run."""
    await workload_pool.open()
    await control_pool.open()


async def close_joined_host_pools(
    host: AgentHost | None,
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
) -> None:
    if host is None or host.resources_joined:
        await close_host_pools(workload_pool, control_pool)
    else:
        host_log.error("[agent-host] resource join unfinished; keeping pools open until hard exit")


async def close_process_owners(
    host: AgentHost | None,
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    installation: Installation | None,
    health: asyncio.Server | None,
    beat: asyncio.Task[Exception | None] | None,
    beat_tasks: asyncio.TaskGroup | None,
    *,
    clients: ClientSet | None = None,
    instance_lock: int | None = None,
    primary: BaseException | None = None,
) -> None:
    """Collect process owners without replacing a failure from boot or turn drain."""
    try:
        async with contextlib.AsyncExitStack() as cleanup:
            if instance_lock is not None:
                cleanup.callback(release_instance_lock, instance_lock)
            cleanup.callback(remove_pidfile, _pidfile())
            cleanup.push_async_callback(close_joined_host_pools, host, workload_pool, control_pool)
            if host is None and clients is not None:
                cleanup.callback(clients.close)
            if installation is not None:
                cleanup.callback(installation.sampling.close)
            if health is not None:
                cleanup.push_async_callback(stop_health_server, health)
            cleanup.push_async_callback(close_ownership_beat, beat, beat_tasks)
    except BaseException as secondary:
        if primary is None:
            raise
        primary.add_note(f"Host process cleanup also failed: {secondary!r}")


# meiva:note
# Whether a host is already running; a live pid whose argv does not name this module is recycled.
def is_running() -> bool:
    return pidfile_holds_daemon(_pidfile(), _MODULE)


def boot_handles(
    db: Database, config: ConfigBoot, rng: random.Random
) -> tuple[
    AsyncConnectionPool[psycopg.AsyncConnection],
    AsyncConnectionPool[psycopg.AsyncConnection],
    EventBus,
    Database,
]:
    """The turn/checkpoint pool, the reserved control pool, the event bus and the database of one host."""
    bus = EventBus(
        EventBusConfig(
            redis_url=config.view.data_plane.redis_url,
            events_channel=config.view.data_plane.events_channel,
        ),
        rng=rng,
    )
    bounds = pool_bounds(config)
    return build_shared_pool(db, bounds), build_control_pool(db, bounds), bus, db


# meiva:note
# Run dispatcher siblings, joined before the owned heartbeat and turn drain.
async def run_dispatcher_group(
    loops: dict[str, Coroutine[object, object, None]],
    dispatch: Coroutine[object, object, None],
) -> None:
    try:
        async with asyncio.TaskGroup() as background:
            for name, loop in loops.items():
                background.create_task(loop, name=name)
            await dispatch
            # meiva:note
            # The dispatcher runs until cancelled; a return would leave the group waiting on loops that never end.
            raise RuntimeError("wake dispatcher exited without cancellation")
    finally:
        dispatch.close()


def _state_gated_loader() -> Callable[..., object]:
    """The plugin loader, given the agent kernel's plugin-state admission check."""
    from agent.state import plugin_state_schema
    from meiva.sdk_surface.extensions import load_extensions

    return partial(load_extensions, validate_state=plugin_state_schema)


# meiva:note
# Enter the service-owned heartbeat group, settle boot, open health and serve until exit.
async def serve_host_lifecycle(
    host: AgentHost,
    scheduler: TurnScheduler,
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    installation: Installation,
    *,
    beat_coro: Coroutine[object, object, Exception | None],
    settle_coro: Coroutine[object, object, Collection[int]],
    open_health: Coroutine[object, object, asyncio.Server],
    serve_dispatch: Coroutine[object, object, None],
    write_restart: Callable[[int], Awaitable[OwedRestart]],
    clients: ClientSet | None = None,
    instance_lock: int | None = None,
) -> None:
    beat: asyncio.Task[Exception | None] | None = None
    beat_tasks: asyncio.TaskGroup | None = None
    health: asyncio.Server | None = None
    try:
        beat_tasks = asyncio.TaskGroup()
        await beat_tasks.__aenter__()
        beat = beat_tasks.create_task(beat_coro, name="host-ownership-heartbeat")
        settled = await settle_coro
        logger.info("hosted boot settle: settled {n} stale running row(s)", n=len(settled))
        health = await open_health
        logger.info("hosted agent-runner started", event="host_started")
        try:
            await serve_until_exit(
                serve_dispatch,
                turns=scheduler,
                progress=host.turn_progress,
                write_restart=write_restart,
            )
        finally:
            try:
                await _close_host_runtime(host, scheduler, beat, beat_tasks)
            finally:
                beat = beat_tasks = None
    finally:
        for coro in (beat_coro, settle_coro, open_health, serve_dispatch):
            coro.close()
        await close_process_owners(
            host,
            workload_pool,
            control_pool,
            installation,
            health,
            beat,
            beat_tasks,
            clients=clients,
            instance_lock=instance_lock,
            primary=sys.exception(),
        )
        host_log.info("[agent-host] daemon stopped")


# meiva:note
# The host process's random generator, built once per run for its sampling owner. Sampling is not a
# secret, so it is a plain `random.Random`.
def _new_rng() -> random.Random:
    return random.Random()  # noqa: S311


# meiva:note
# What this process looks like at the moment it spawns an exec child: the environment the child
# inherits, its working directory, the interpreter it re-executes, the checkout that interpreter
# belongs to and the one extra checkout its editable install may name. The host reads it once at
# boot and hands it to `build_graph`, which gives it to the exec process.
def read_parent_facts() -> ParentFacts:
    return ParentFacts(
        environ=dict(os.environ),
        cwd=Path.cwd().resolve(),
        executable=sys.executable,
        source_root=editable_install.current_interpreter_source_root(sys.executable),
        editable_roots=(Path.home() / "Meiva",),
    )


async def run(
    *,
    config: ConfigBoot,
    database: Callable[[], Database],
    machine: MachineIdentityReader,
    home: Path,
    user_home: Path,
    clients: ClientSet | None = None,
    on_clients_owned: Callable[[], None] | None = None,
) -> None:
    """Boot the host and serve wakes in the order documented by this module."""
    assert_clock_lattice(
        clock_values(
            config.view.daemon.wedged_agent_inbound_age_seconds,
            config.view.sandbox.exec_node_timeout_seconds,
        )
    )
    instance_lock = take_instance_lock(home)
    _claim_pidfile()
    from agent.graph import build_graph  # pyright: ignore[reportUnknownVariableType]
    from agent.process_boot import init_process_scope, land_cluster_extensions, process_trace_config
    from base.telemetry.otlp.trace_readiness import PROCESS_RECORDING

    rng = _new_rng()
    workload_pool, control_pool, bus, db = boot_handles(database(), config, rng)
    secure_rng = secrets.SystemRandom()
    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    installation: Installation | None = None
    host: AgentHost | None = None
    handed_off = False
    try:
        if on_clients_owned is not None:
            on_clients_owned()
        init_process_scope(
            machine=machine,
            home=home,
            trace=process_trace_config(config),
            environ=os.environ,
            readiness=PROCESS_RECORDING,
        )
        land_cluster_extensions(db, home=home)
        local_machine = machine.name()
        await open_host_pools(workload_pool, control_pool)
        if clients is None:
            clients = clients_over(config, machine, lambda: db)
        installation = load_installation(
            config, clients=clients, load_extensions=_state_gated_loader(), rng=rng, home=home
        )
        checkpointer = await build_checkpointer(
            workload_pool,
            [cls for _plugin, cls in installation.registry.state_classes()],
            checkpoint_interval=config.view.agent.checkpoint_interval,
        )
        stamping = handoff_source(config)
        host = AgentHost(
            pool=workload_pool,
            control_pool=control_pool,
            checkpointer=checkpointer,
            graph=build_graph(
                checkpointer, installation.registry, parent=read_parent_facts(), rng=secure_rng
            ),
            machine=machine,
            bus=bus,
            db=db,
            catalog=installation.require_catalog(),
            policy=host_policy(
                config,
                keys=installation.require_provider_keys(),
                rng=SecureRandom(secure_rng),
                pid=os.getpid(),
                user_home=user_home,
            ),
            handoff_stamping=stamping,
            clients=clients,
            extensions=installation.registry,
            commands=CommandCatalog.system(
                skills=partial(meiva_skills.names_under, home=home), home=home
            ),
            plugin_configs=installation.configs,
            home=home,
            runtime_owner=new_uuid(secure_rng),
        )
        scheduler = TurnScheduler(
            host.run_turn,
            activity_clock=host.last_active_at,
            config_fingerprint=host.turn_fingerprints.get,
            clock=stamping.clock,
            timers=stamping.clock,
        )
        host.bind_wake(scheduler.wake)
        endpoint = _endpoint()
        wait_s = float(config.view.agent.db_notify_wait_timeout_seconds)
        handed_off = True
        await serve_host_lifecycle(
            host,
            scheduler,
            workload_pool,
            control_pool,
            installation,
            beat_coro=ownership_beat_completion(
                liveness,
                host,
                scheduler,
                local_machine,
                bus,
                require_helper_chain=partial(
                    require_helper_parent_chain, pid=os.getpid(), environ=os.environ
                ),
                read_alert_seconds=lambda: config.view.daemon.host_admission_wait_alert_seconds,
            ),
            settle_coro=settle_running_rows_at_start(control_pool, local_machine),
            open_health=start_health_server(
                "agent_host",
                endpoint.health_port,
                image=process_context.loaded_code_image(),
                liveness=liveness,
                extra_routes={
                    ("GET", "/stats"): stats_route(host, scheduler, pid=os.getpid(), home=home),
                    ("POST", "/cancel-turn"): cancel_turn_route(scheduler),
                },
            ),
            serve_dispatch=run_dispatcher_group(
                background_loops(
                    control_pool,
                    db,
                    installation=installation,
                    config=config,
                    plugins_root=external_plugin_read_root(home=home),
                    stdout_log=stdout_log_path(logs_dir=logs_dir(home=home)),
                ),
                InboundWakeDispatcher(
                    bus,
                    scheduler,
                    timers=stamping.clock,
                    pending_scan=host.pending_inbound_wakes,
                    scan_interval_s=wait_s,
                    subscription_read_timeout_s=wait_s,
                ).run(),
            ),
            write_restart=partial(write_exit_restart, db, bus),
            clients=clients,
            instance_lock=instance_lock,
        )
    finally:
        if not handed_off:
            await close_process_owners(
                host,
                workload_pool,
                control_pool,
                installation,
                None,
                None,
                None,
                clients=clients,
                instance_lock=instance_lock,
                primary=sys.exception(),
            )
            host_log.info("[agent-host] daemon stopped")


def require_helper_parent_chain(*, pid: int, environ: Mapping[str, str]) -> None:
    if parent_chain_intact(pid, environ=environ):
        return
    logger.warning(
        "[agent-host] permissions helper parent chain broken, self-terminating for helper respawn",
        event="helper_chain_broken",
        process="agent_host",
        detail=f"agent host pid {pid} is no longer under the permissions helper "
        f"that spawned it (marker pid {helper_marker_pid(environ) or 'absent'})",
    )
    os._exit(HELPER_CHAIN_BROKEN_EXIT_CODE)


def main() -> None:
    """Entry point: SDK posture, schema gate, logging, graceful shutdown, then the loop."""
    process_context.bind_host_process()
    from base.deploy.schema.migrations import assert_schema_current

    config, home = boot_process()
    require_helper_parent_chain(pid=os.getpid(), environ=os.environ)
    assert_schema_current(config.view.data_plane.db_url)
    machine = ConfiguredMachineIdentity(config)
    version = CodeVersion(process_context.loaded_code_image())
    gate = ProcessDbGate(version=version.get, process="agent_host")

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=home),
        )

    clients = clients_over(config, machine, database)
    clients_handed_off = False

    def hand_off_clients() -> None:
        nonlocal clients_handed_off
        clients_handed_off = True

    initialize_logging(clients, machine, process_context.loaded_code_image(), home)
    code = 0
    # Runner avoids automatic shutdown_default_executor joins of to_thread
    # recorders. Explicit drain reaches run's cleanup; hard exit skips executor teardown.
    runner = asyncio.Runner()
    try:
        install_graceful_shutdown("agent_host")
        runner.run(
            run(
                config=config,
                database=database,
                machine=machine,
                home=home,
                user_home=Path.home(),
                clients=clients,
                on_clients_owned=hand_off_clients,
            )
        )
    except KeyboardInterrupt as interrupted:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        host_log.info("[agent-host] interrupted, shutting down")
        # Explicit task drain reaches run's health/turn/pool/pidfile cleanup
        # without joining default-executor workers.
        failures = cancel_and_drain(runner, interrupted_by=interrupted)
        if failures:
            host_log.error("[agent-host] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        host_log.exception("[agent-host] fatal error, shutting down")
        code = 1
    finally:
        # A running host retains its clients when a turn cannot join. Only a
        # failure before that handoff belongs to this boot scope.
        if not clients_handed_off:
            try:
                clients.close()
            except BaseException as secondary:
                primary = sys.exception()
                if primary is not None:
                    primary.add_note(f"Host boot cleanup also failed: {secondary!r}")
                host_log.exception("[agent-host] process client cleanup failed")
                code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main()
