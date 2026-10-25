"""Gateway process entry point, kept separate from ASGI application wiring."""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import signal
import socket
import sys
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import uvicorn
from fastapi import FastAPI
from uvicorn.config import STARTUP_FAILURE

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.port_preflight import serves_off_box
from base.cluster.transport_encryption import verify_transport_encryption_declaration
from base.config import ConfigBoot
from base.deploy.schema.migrations import assert_schema_current
from base.host.net.auth import require_cluster_secret
from base.host.system.hostport import Host
from base.native_process.os_platform import raise_fd_limit
from base.paths import meiva_home
from base.telemetry.process_logging import init_gateway_process
from gateway.cluster.process_boot import LOADED_IMAGE, GatewayProcess
from gateway.cluster.serve_plan import ServePlan, serve_plan
from gateway.http.middleware import stopping

_log = logging.getLogger(__name__)
_GATEWAY_UVICORN_WORKERS = 1


class GatewayServer(uvicorn.Server):
    """uvicorn's server, marking its shutdown for long-lived streams as it begins.

    uvicorn cancels an unfinished response only when `timeout_graceful_shutdown`
    runs out, so an SSE stream that never ends by itself holds every stop for that
    whole budget (and meiva-root's window for this unit is derived from it). Marking
    the shutdown first lets the streams end within one poll tick; the budget stays
    the bound for whatever does not end.
    """

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        stopping.mark_stopping()
        await super().shutdown(sockets)


def serve(kwargs: dict[str, Any]) -> None:
    """`uvicorn.run` with the gateway's own server class.

    `uvicorn.run` always builds the stock `Server`, so this is its single-worker
    path with `GatewayServer`, including the startup-failure exit code root's
    restart policy reads. Hot reload (dev only) keeps `uvicorn.run`: its reloader
    builds its own server in a child process.
    """
    if kwargs["reload"]:
        uvicorn.run(**kwargs)
        return
    config = uvicorn.Config(**kwargs)
    config.load_app()
    server = GatewayServer(config)
    with suppress(KeyboardInterrupt):
        server.run()
    if not server.started:
        sys.exit(STARTUP_FAILURE)


# meiva:note
# Prepare the gateway process and start its ASGI server.
#
# The builder borrows this entry's process. Development reload creates its
# owned app in the new worker through the environment factory. The caller
# boots `config`; the decisions that follow are `serve_plan`'s.
def main(*, config: ConfigBoot, build_app: Callable[[GatewayProcess], FastAPI]) -> None:
    machine = ConfiguredMachineIdentity(config)
    plan = serve_plan(
        serves_gateway="gateway" in machine.role(),
        off_box=serves_off_box(machine=machine),
        reload=config.view.gateway.gateway_reload,
    )
    # The API always authenticates: a gateway without a human secret refuses to
    # serve, before it touches the database or binds a port.
    if plan.require_cluster_secret:
        require_cluster_secret(config.view.data_plane.cluster_secret, where="this gateway")
    assert_schema_current(config.view.data_plane.db_url)

    # pidfile — `services/supervision/healthchecks/gateway.py` uses it to probe.
    # SIGKILL does not trigger atexit; the healthcheck uses kill -0 to
    # judge liveness, so a stale pidfile is not fatal (kill -0 fail -> restart).
    pidfile = config.view.services.gateway_pidfile
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))

    def _cleanup_pidfile() -> None:
        with suppress(OSError):
            pidfile.unlink(missing_ok=True)

    atexit.register(_cleanup_pidfile)

    # Raise the fd limit: it covers the gateway's own SSE long connections +
    # Redis pubsub + HTTP keepalive, which on a long run would otherwise blow
    # the launchd 256 default (errno 24 -> requests connection-reset, surfaced
    # in the frontend as "Failed to fetch"), and the session children it spawns
    # inherit the raised ceiling.
    raise_fd_limit(65536)

    process = GatewayProcess(
        home=meiva_home(), config=config, image=LOADED_IMAGE, machine=machine, host=Host.system()
    )
    with process.lifetime():
        _serve_process(process, plan, build_app)


def _serve_process(
    process: GatewayProcess, plan: ServePlan, build_app: Callable[[GatewayProcess], FastAPI]
) -> None:
    """Serve one owned entry; the ASGI lifespan borrows its existing root."""
    init_gateway_process(
        producer=process.event_pipeline,
        machine_reader=process.machine.name,
        image=process.image,
    )

    # Raise the cluster's minimum code version to this gateway's own: a process
    # left running older code (a runner offline during the update) then refuses
    # to write. After the schema assertion and the logger init, so a refusal or
    # meiva:note
    # a failure is logged.
    if plan.raise_min_code_version:
        process.gate.raise_min_code_version(process.database())

    # Thread dump on SIGUSR1: the watchdog's gateway healthcheck sends this
    # before respawning a frozen gateway, so a stall lands a stack trace in
    # the pane log instead of a silent black box (2026-08-03: 13 freezes in
    # 8h, none left a trace between the last log line and the kill). uvicorn
    # does not touch SIGUSR1, so the registration survives into the loop.
    faulthandler.register(signal.SIGUSR1)

    # reload defaults to False — prod-safe. reload=True forks workers via
    # multiprocessing.spawn, and the worker's `PPID=1` is fully detached
    # from the session: when the session closes the worker does
    # not die, leaving a zombie holding :8000; the next graceful kill on
    # a fleet update cannot catch it, and the new gateway boot gets
    # [Errno 48] Address already in use. For dev hot-reload, set
    # MEIVA_GATEWAY_RELOAD=1 (usually in a dev clone's .env or shell). Reload
    # mode binds through uvicorn's own bind_socket, which maps "" to a
    # v4-only wildcard — dev-only, and browsers fall back from the refused
    # IPv6 dial instantly.
    if plan.transport_declaration_required:
        verify_transport_encryption_declaration(
            plan.bind_host, process.config.view.data_plane.transport_encryption, authenticated=True
        )
    _log.warning("gateway starts with one uvicorn worker because rate limiters are process-local")
    if plan.app_factory_path is not None:
        # The parent never constructs the ASGI app a reload worker will serve.
        serve(
            serve_kwargs(
                host=plan.bind_host,
                app=plan.app_factory_path,
                factory=True,
                config=process.config,
            )
        )
    else:
        serve(
            serve_kwargs(
                host=plan.bind_host, app=build_app(process), factory=False, config=process.config
            )
        )


def serve_kwargs(*, host: str, app: Any, factory: bool, config: ConfigBoot) -> dict[str, Any]:
    """Assemble the uvicorn launch parameters for the gateway ASGI server.

    The single assembly point of the launch contract: ``main()`` hands the dict
    straight to ``serve``, and the shutdown regression
    (``gateway/tests/bootstrap/test_server_shutdown.py``) starts a real child-process
    server from the same dict — only the bind address, the app path and the
    port are swapped — so a field dropped here (in particular
    ``timeout_graceful_shutdown``) fails a real server, not just a mock.

    ``timeout_graceful_shutdown`` bounds uvicorn's connection-drain phase.
    Uvicorn's default is ``None``, and an unfinished streaming response then
    holds the drain in "Waiting for connections to close" forever — the
    2026-09-17 gateway stall, ended only by a forced kill. Once the budget
    elapses uvicorn cancels the remaining request/stream tasks and runs the
    lifespan shutdown, so a stuck stream costs at most the configured budget,
    never the maintenance-stop deadline.
    """
    reload = config.view.gateway.gateway_reload
    return {
        "app": app,
        "factory": factory,
        "host": host,
        "port": config.view.gateway.gateway_port,
        "reload": reload,
        "reload_dirs": ["gateway", "base", "meiva", "agent"] if reload else None,
        # log_config=None: uvicorn's default LOGGING_CONFIG dictConfig would
        # clobber the root-handler install (`_StdlibInterceptHandler`) that
        # init_gateway_process set up above, sending uvicorn's own records
        # (ASGI tracebacks, startup/shutdown) to a bare stderr handler instead
        # of through loguru → gateway.log + the events pipeline. With None,
        # uvicorn leaves the logging system alone; uvicorn.error propagates to
        # the root intercept handler, and uvicorn.access is gated to WARNING in
        # `install_stdlib_intercept` (per-request INFO is noise). #970: an
        # unhandled ASGI exception used to land only in the session log and die
        # with the session — now it reaches gateway.log and the events pipeline.
        "log_config": None,
        "workers": _GATEWAY_UVICORN_WORKERS,
        "timeout_graceful_shutdown": config.view.gateway.gateway_graceful_shutdown_timeout_seconds,
    }
