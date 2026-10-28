"""IM Bridge daemon — runs every configured IM channel adapter.

Each IM channel is one adapter (service) sharing the bridge core: message
envelope, command routing, per-channel session state, and SSE subscription
push. Channels disabled by configuration or without configured credentials remain
inactive. Import, configuration and programming errors fail the daemon.

Usage:
    .venv/bin/python -m services.entrypoints.im_bridge.daemon
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from psycopg.errors import ConnectionDoesNotExist, ConnectionFailure
from psycopg_pool import ConnectionPool, PoolTimeout

from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import GatewayAccess, daemon_acceptance, gateway_auth_headers
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.daemon.health import Liveness, start_health_server, stop_health_server
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.env.registry import network_proxy_configured
from base.host.exit_codes import hard_exit as _hard_exit
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from base.telemetry.pipeline_build import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from services.entrypoints.im_bridge.adapters.run_plan import AdapterPlan, exit_code, plan_adapters
from services.entrypoints.im_bridge.alert_outbound import AlertOutboundBridge
from services.entrypoints.im_bridge.config import (
    FeishuCredentialsConfig,
    ImBridgeConfig,
    TelegramCredentialsConfig,
    state_dir_of,
)
from services.entrypoints.im_bridge.core import IMBridgeCore
from services.entrypoints.im_bridge.gateway_client import GatewayClient
from services.entrypoints.im_bridge.outbound.health import push_health_loop
from services.entrypoints.im_bridge.types import NETWORK_ERRORS, IMAdapter
from services.pidfile import acquire_pidfile, pidfile_holds_daemon, remove_pidfile

_log = logging.getLogger("services.entrypoints.im_bridge.daemon")

# Staleness ceiling for the healthz liveness — generous: the daemon is
# mostly idle waiting on long polls; only a wedged event loop should trip it.
_LIVENESS_TIMEOUT_S = 120.0


# meiva:note
# The cadences of the daemon's background loops. The main task waits on an event once the
# adapters are launched, so a background task carries the liveness heartbeat; its interval sits
# well under the staleness ceiling. The notice and outbound loops pull on a short fixed round.
@dataclass(frozen=True)
class DaemonTimings:
    liveness_beat_s: float = 30.0
    notice_poll_s: float = 3.0
    outbound_round_s: float = 3.0


DEFAULT_TIMINGS = DaemonTimings()


def _endpoint(config: ConfigBoot, *, runtime: Path) -> ServiceEndpoint:
    runtime.mkdir(parents=True, exist_ok=True)
    return ServiceEndpoints.from_boot(config, runtime=runtime).of("im_bridge")


def _pidfile(config: ConfigBoot, *, runtime: Path) -> Path:
    return _endpoint(config, runtime=runtime).pidfile


def write_pidfile(config: ConfigBoot, *, runtime: Path) -> None:
    if not acquire_pidfile(
        _pidfile(config, runtime=runtime), "services.entrypoints.im_bridge.daemon", pid=os.getpid()
    ):
        _log.info(
            "[im_bridge] daemon already running (pidfile=%s), exiting",
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
        _pidfile(config, runtime=runtime), "services.entrypoints.im_bridge.daemon"
    )


# -- composition root --------------------------------------------------------
# meiva:note
# This module is the composition root of the package: the only place that reads configuration,
# from the `ConfigBoot` the process owns. It builds the configuration slices from the field
# values and hands each to the component that uses it.


def im_bridge_config(config: ConfigBoot) -> ImBridgeConfig:
    view = config.view
    return ImBridgeConfig(
        im_disabled_adapters=tuple(view.services.im_disabled_adapters),
        im_send_retry_delays=tuple(view.services.im_send_retry_delays),
        im_push_retry_backoff_seconds=view.services.im_push_retry_backoff_seconds,
        im_push_retry_jitter_seconds=view.services.im_push_retry_jitter_seconds,
        im_sse_read_timeout_seconds=view.services.im_sse_read_timeout_seconds,
        im_bridge_timeline_window=view.services.im_bridge_timeline_window,
        im_bridge_replay_messages=view.services.im_bridge_replay_messages,
        im_bridge_notice_reply_window_seconds=view.services.im_bridge_notice_reply_window_seconds,
        im_bridge_notice_open_limit=view.services.im_bridge_notice_open_limit,
        im_push_health_channels=tuple(view.services.im_push_health_channels),
        im_push_health_interval_seconds=view.services.im_push_health_interval_seconds,
        notices_open_default_limit=view.display.notices_open_default_limit,
    )


def telegram_config(config: ConfigBoot) -> TelegramCredentialsConfig:
    view = config.view
    return TelegramCredentialsConfig(
        telegram_bot_token=view.telegram.telegram_bot_token,
        telegram_owner_id=view.telegram.telegram_owner_id,
        telegram_poll_timeout_seconds=view.telegram.telegram_poll_timeout_seconds,
        telegram_reconnect_base_delay_seconds=view.telegram.telegram_reconnect_base_delay_seconds,
        telegram_reconnect_max_delay_seconds=view.telegram.telegram_reconnect_max_delay_seconds,
    )


def feishu_config(config: ConfigBoot) -> FeishuCredentialsConfig:
    view = config.view
    return FeishuCredentialsConfig(
        feishu_app_id=view.feishu.feishu_app_id,
        feishu_app_secret=view.feishu.feishu_app_secret,
        feishu_rest_timeout_seconds=view.feishu.feishu_rest_timeout_seconds,
        feishu_poll_interval_seconds=view.feishu.feishu_poll_interval_seconds,
        feishu_poll_chat_id=view.feishu.feishu_poll_chat_id,
        delivery_watchdog_stale_claimed_threshold_seconds=(
            view.daemon.delivery_watchdog_stale_claimed_threshold_seconds
        ),
    )


def gateway_client(
    config: ImBridgeConfig,
    *,
    gateway_url: str,
    access: GatewayAccess,
    rng: random.Random,
) -> GatewayClient:
    return GatewayClient(
        config, gateway_url=gateway_url, auth_headers=gateway_auth_headers(access), rng=rng
    )


# meiva:note
# What the adapters are built from: the configuration slices of the adapters that take one, the
# bridge's state directory, whether the machine names an outbound proxy for the Feishu websocket
# handshake, and the process's clock, random generator and id source that the adapters which
# stamp times, draw jitter or mint ids use.
@dataclass(frozen=True)
class AdapterInputs:
    telegram: TelegramCredentialsConfig
    feishu: FeishuCredentialsConfig
    state_dir: Path
    proxy_configured: bool
    clock: Clock
    rng: random.Random


def adapter_inputs(
    config: ConfigBoot,
    *,
    state_dir: Path,
    clock: Clock,
    rng: random.Random,
) -> AdapterInputs:
    environment = config.process_environment()
    return AdapterInputs(
        telegram=telegram_config(config),
        feishu=feishu_config(config),
        state_dir=state_dir,
        proxy_configured=network_proxy_configured(environment),
        clock=clock,
        rng=rng,
    )


# meiva:note
# One static import per channel: a disabled adapter's module is never imported.
def _build_adapter(name: str, core: IMBridgeCore, inputs: AdapterInputs) -> IMAdapter:
    if name == "telegram":
        from services.entrypoints.im_bridge.adapters import telegram

        return telegram.ADAPTER_CLASS(core, inputs.telegram, state_dir=inputs.state_dir)
    if name == "weixin":
        from services.entrypoints.im_bridge.adapters import weixin

        return weixin.ADAPTER_CLASS(
            core,
            state_dir=inputs.state_dir,
            clock=inputs.clock,
            rng=inputs.rng,
        )
    if name == "feishu":
        from services.entrypoints.im_bridge.adapters import feishu

        return feishu.ADAPTER_CLASS(
            core,
            inputs.feishu,
            state_dir=inputs.state_dir,
            proxy_configured=inputs.proxy_configured,
            cursor_store=core.cursor_store,
            clock=inputs.clock,
        )
    raise ValueError(f"unknown im_bridge adapter {name!r}")


# meiva:note
# Compose the configured adapters and register them with the core; invalid configuration or
# imports fail startup.
def load_adapters(core: IMBridgeCore, plan: AdapterPlan, inputs: AdapterInputs) -> list[IMAdapter]:
    for name in plan.disabled:
        _log.info("im_bridge: adapter %s disabled by config (MEIVA_IM_DISABLED_ADAPTERS)", name)
    loaded: list[IMAdapter] = []
    for name in plan.load:
        adapter = _build_adapter(name, core, inputs)
        core.register(adapter)
        loaded.append(adapter)
        _log.info("im_bridge: adapter %s loaded", name)
    return loaded


async def liveness_loop(liveness: Liveness, *, interval_s: float) -> None:
    """Beat the healthz liveness while the daemon serves.

    The main loop's work happens in adapter background tasks and the final
    ``asyncio.Event().wait()`` never iterates, so this task is the heartbeat:
    a wedged event loop stops scheduling it and the staleness ceiling trips.
    """

    while True:
        liveness.beat()
        await asyncio.sleep(interval_s)


async def notice_loop(core: IMBridgeCore, *, interval_s: float) -> None:
    """Poll the gateway for new fleet notices and push them to the owner
    chat (Task #884). Cursor + filter persist across restarts."""

    while True:
        await core.notice_bridge.poll_once()
        await asyncio.sleep(interval_s)


async def timeline_outbound_loop(
    core: IMBridgeCore, alerts: AlertOutboundBridge, *, interval_s: float
) -> None:
    """One service-owned dispatcher and periodic committed-tail wakeup."""
    while True:
        try:
            await alerts.poll_once()
            await core.poll_timeline_outbound()
        except (
            *NETWORK_ERRORS,
            ConnectionDoesNotExist,
            ConnectionFailure,
            PoolTimeout,
        ) as exc:
            _log.warning("im_bridge: outbound round failed class=%s", type(exc).__name__)
        await asyncio.sleep(interval_s)


class NotifyTarget(Protocol):
    async def notify_user(self, text: str) -> dict[str, str]: ...


async def handle_send(
    core: NotifyTarget,
) -> Callable[[bytes], Coroutine[Any, Any, tuple[int, bytes, str]]]:
    """Route handler for the daemon's ``POST /send`` RPC (ops-alerts fan-out).

    Body: ``{"text": str}`` — fanned out to every loaded adapter's owner chat
    via ``core.notify_user``. The gateway calls this with the cluster secret
    as Bearer (the health server's ``auth_digests``). Returns per-channel
    results; a channel that failed to send is reported, not fatal. When
    EVERY channel failed (or none is loaded) the route answers 502 instead
    of 200 — the caller (base/telemetry/alerts/__init__.py) keys ``notified_at`` off the status
    code, and a fake 200 would stamp a message that never reached the user.
    """

    async def handle(body: bytes) -> tuple[int, bytes, str]:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return 400, b'{"error": "invalid json body"}', "application/json"
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, b'{"error": "text (non-empty string) required"}', "application/json"
        results = await core.notify_user(text)
        delivered = any(v == "ok" for v in results.values())
        if not delivered:
            # Nothing reached the user — report failure so the caller does not
            # treat the fan-out as delivered (base/telemetry/alerts/__init__.py keeps notified_at NULL
            # and retries on the next Grafana re-send).
            return 502, json.dumps({"results": results}).encode(), "application/json"
        return 200, json.dumps({"results": results}).encode(), "application/json"

    return handle


def _begin_shutdown(adapters: list[IMAdapter]) -> list[BaseException]:
    errors: list[BaseException] = []
    for adapter in adapters:
        try:
            adapter.begin_shutdown()
        except BaseException as error:
            errors.append(error)
    return errors


# meiva:note
# Attempt every owned resource once, in order, returning the faults for the daemon to expose.
async def stop_resources(
    core: IMBridgeCore | None,
    adapters: list[IMAdapter],
    health: asyncio.Server | None,
    pool: ConnectionPool,
    *,
    config: ConfigBoot,
    runtime: Path,
) -> list[BaseException]:
    errors: list[BaseException] = []
    if core is not None:
        try:
            await core.stop()
        except BaseException as error:
            errors.append(error)
    for adapter in adapters:
        try:
            await adapter.stop()
        except BaseException as error:
            errors.append(error)
    if health is not None:
        try:
            await stop_health_server(health)
        except BaseException as error:
            errors.append(error)
    for close in (pool.close, partial(_remove_pidfile, config, runtime=runtime)):
        try:
            close()
        except BaseException as error:
            errors.append(error)
    return errors


# meiva:note
# The resources `_start` has brought up so far, so the cleanup in `run` stops exactly those.
@dataclass
class _Owned:
    core: IMBridgeCore | None = None
    health: asyncio.Server | None = None
    adapters: list[IMAdapter] = field(default_factory=list[IMAdapter])


# meiva:note
# Bring the daemon up inside the loops' task group and park: core, health server, adapters and
# the background loops, in that order. Whatever it has started is recorded in `owned`.
async def _start(
    loops: asyncio.TaskGroup,
    owned: _Owned,
    *,
    process_config: ConfigBoot,
    runtime: Path,
    db_pool: ConnectionPool,
    image: LoadedCommit,
    timings: DaemonTimings,
    clock: Clock,
    rng: random.Random,
) -> None:
    config = im_bridge_config(process_config)
    access = gateway_access_from_boot(process_config)
    # meiva:note
    # The runtime directory is `<home>/run`, so its parent is the Meiva home.
    home = runtime.parent
    state_dir = state_dir_of(home)
    client = gateway_client(
        config, gateway_url=process_config.view.gateway.gateway_url, access=access, rng=rng
    )
    core = owned.core = IMBridgeCore(
        config,
        client,
        db_pool=db_pool,
        tasks=loops,
        state_dir=state_dir,
        clock=clock,
        rng=rng,
    )
    alerts = AlertOutboundBridge(
        core.outbound_store, core.adapters, enabled=process_config.view.alerts.im_notify_enabled
    )
    liveness = Liveness(_LIVENESS_TIMEOUT_S)
    endpoint = _endpoint(process_config, runtime=runtime)
    await asyncio.to_thread(core.notice_bridge.initialize_poll)
    owned.health = await start_health_server(
        "im_bridge",
        endpoint.health_port,
        liveness=liveness,
        extra_routes={
            ("POST", "/send"): await handle_send(core),
            ("POST", "/send/alert-outbound"): alerts.handle,
        },
        auth_digests=daemon_acceptance(access, home=home),
        image=image,
    )
    _log.info("[im_bridge] healthz listening on :%s", endpoint.health_port)
    adapters = owned.adapters = load_adapters(
        core,
        plan_adapters(frozenset(config.im_disabled_adapters)),
        adapter_inputs(process_config, state_dir=state_dir, clock=clock, rng=rng),
    )
    if not adapters:
        _log.warning("im_bridge: no adapters loaded — nothing to serve")
    await core.restore_subscriptions()
    loops.create_task(liveness_loop(liveness, interval_s=timings.liveness_beat_s))
    loops.create_task(notice_loop(core, interval_s=timings.notice_poll_s))
    loops.create_task(push_health_loop(core.outbound_store, config, clock=clock))
    await asyncio.gather(*(a.start(loops) for a in adapters))
    if adapters:
        loops.create_task(timeline_outbound_loop(core, alerts, interval_s=timings.outbound_round_s))
    await asyncio.Event().wait()


async def run(
    *,
    process_config: ConfigBoot,
    runtime: Path,
    database: Callable[[], Database],
    image: LoadedCommit,
    timings: DaemonTimings = DEFAULT_TIMINGS,
    clock: Clock,
    rng: random.Random,
) -> None:
    """Start the daemon: healthz -> pidfile -> load adapters -> serve."""
    if _is_running(process_config, runtime=runtime):
        _log.info(
            "[im_bridge] daemon already running (pidfile=%s), exiting",
            _pidfile(process_config, runtime=runtime),
        )
        sys.exit(1)

    write_pidfile(process_config, runtime=runtime)
    _log.info("[im_bridge] pidfile written: %s", _pidfile(process_config, runtime=runtime))

    # The notice bridge reads agent_notices directly (R3 door ④ — decoupled
    # from gateway availability, so a paused cluster cannot stall notice
    # delivery); the pool is created here and owned by the daemon.
    db_pool = database().pool()
    owned = _Owned()
    cleanup_errors: list[BaseException] = []
    primary: BaseException | None = None
    try:
        async with asyncio.TaskGroup() as loops:
            try:
                await _start(
                    loops,
                    owned,
                    process_config=process_config,
                    runtime=runtime,
                    db_pool=db_pool,
                    image=image,
                    timings=timings,
                    clock=clock,
                    rng=rng,
                )
            finally:
                cleanup_errors.extend(_begin_shutdown(owned.adapters))
    except BaseException as error:
        primary = error
    finally:
        # Children have drained; every shared resource is attempted even after a stop fault.
        cleanup_errors.extend(
            await stop_resources(
                owned.core,
                owned.adapters,
                owned.health,
                db_pool,
                config=process_config,
                runtime=runtime,
            )
        )
        _log.info("[im_bridge] daemon stopped")
    if cleanup_errors:
        errors = ([primary] if primary is not None else []) + cleanup_errors
        raise BaseExceptionGroup("IM daemon failure and cleanup faults", errors)
    if primary is not None:
        raise primary


def gate_httpx_info_logs() -> None:
    """Gate httpx's per-request INFO lines in this process.

    httpx logs ``HTTP Request: GET <url>`` at INFO, and the Telegram Bot API
    carries the bot token inside the URL path (``.../bot<id>:<token>/...``):
    every long poll therefore persisted the live token into this daemon's
    structured log in cleartext (task #4067). Raising this one third-party
    logger to WARNING keeps genuine library warnings/errors while dropping
    the URL-bearing INFO lines. The adapters separately keep httpx
    *exceptions* (which can embed the same URL) out of their own log calls.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    image = LoadedCommit.capture()
    machine_config = ConfigBoot()
    machine_config.boot()
    machine = ConfiguredMachineIdentity(machine_config)
    process_home = meiva_home().resolve()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="im_bridge")

    def database() -> Database:
        return Database(
            db_config_from_boot(machine_config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=process_home),
        )

    pipeline = build_pipeline(database=database, machine=machine, home=process_home)
    init_gateway_process(
        name="im_bridge",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
        home=process_home,
    )
    gate_httpx_info_logs()
    install_graceful_shutdown("im_bridge")
    crashed = interrupted = False
    drain_failures = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — adapters hand blocking REST calls to it via `asyncio.to_thread`
    # — and a stop signal must never wait on those (see `_hard_exit`). The
    # runner is therefore never closed: after the explicit drain below,
    # teardown is skipped by the hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(
            run(
                process_config=machine_config,
                runtime=process_home / "run",
                database=database,
                image=image,
                clock=Clock(clock_config_from_boot(machine_config), timers=SYSTEM_TIMERS),
                rng=random.SystemRandom(),
            )
        )
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[im_bridge] interrupted")
        interrupted = True
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: `run`'s finally still stops the adapters, the
        # liveness/notice tasks and the health server, and closes the DB pool.
        # The executor is deliberately NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[im_bridge] async shutdown failed: %r", failures)
            drain_failures = len(failures)
    except (Exception, BaseExceptionGroup):
        _log.exception("[im_bridge] daemon crashed — uncaught exception escaped run()")
        crashed = True
    _hard_exit(exit_code(crashed=crashed, interrupted=interrupted, drain_failures=drain_failures))


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
