# meiva:note
# The gateway's own machine as a runner whose ops daemon runs in this process.
#
# This module builds the daemon's real serving stack for one fixture scope: the admission state
# (`active_ops`, `requests`, `workers`, the `ops_concurrency` semaphore), the connection pool and the
# executor `services.agent_runner.agent_ops.boot` builds, `ops_route` as the `POST /ops` handler, and
# `base.daemon.health.start_health_server` as the HTTP layer, bound to port 0 on its own event loop.
# One state serves every call the test makes, so in-flight accounting, the concurrency limit and
# the SIGTERM drain run as they do in a deployed runner. The machine is registered in `machines` with
# the listener's address, so a spawn, a lifecycle request or a config read that the gateway forwards to
# its own machine is executed by the real op code against the test's Postgres and Redis, over the same
# HTTP round trip a deployed runner answers. The host effects behind the ops (processes, commands) are
# the `Host` the caller passes, a `fake_host()`: starting a real agent process is not the subject.
#
# The envelopes that reached the listener are recorded in `RunnerOps.calls`, as for a scripted runner;
# a test asserts on them only where the request IS the behavior (a retry that must not re-launch has
# no other observable than the launches that arrived).

from __future__ import annotations

import asyncio
import functools
import json
import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg_pool import ConnectionPool

from base.agents.upload_delivery.paths import MAX_AGENT_UPLOAD_BYTES, MAX_AGENT_UPLOAD_FILES
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import GatewayAccess, MachineIdentityReader
from base.config import get_field
from base.config.service_read import ConfigAuthority
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import start_health_server, stop_health_server
from base.db import Database
from base.host.db_authority.unit import UnitIdentity
from base.host.system.hostport import Host
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home, resolve_meiva_home, run_dir
from gateway.tests.support.runner_ops.ops_stub import OpsCall, RunnerOps, register_machine
from ops.upload_delivery import ReceiverConfig
from services.agent_runner.agent_ops import boot, in_flight
from services.agent_runner.agent_ops.daemon import ops_route
from services.agent_runner.agent_ops.upload_object import upload_object_route
from services.agent_runner.agent_ops.workspace_export import workspace_export_route

__all__ = ["ExecutedOp", "executed_ops", "local_runner"]

_SETUP_TIMEOUT_S = 30.0
_STOP_TIMEOUT_S = 60.0


# meiva:note
# One op the real daemon executed under an idempotency key, as its ledger row stored the outcome.
@dataclass(frozen=True)
class ExecutedOp:
    kind: str
    status: str
    result: dict[str, Any]


# meiva:note
# The ops of `kind` the real daemon executed, oldest first.
#
# `spawn-launch` and `lifecycle` carry a default idempotency key (`ops.cluster.rpc`), so the daemon
# claims a `api_idempotency` row for each and writes the op's outcome into it when the op returns.
# A test reads that ledger to learn what the daemon did, instead of recording what reached it.
def executed_ops(conn: psycopg.Connection, kind: str) -> list[ExecutedOp]:
    rows = conn.execute(
        "SELECT path, op_status, response_body FROM api_idempotency "
        "WHERE method = 'ops' AND path = %s ORDER BY created_at, key",
        (kind,),
    ).fetchall()
    return [
        ExecutedOp(kind=path, status=status, result=cast("dict[str, Any]", body))
        for path, status, body in rows
    ]


# meiva:note
# What one fixture scope's daemon holds: the listener and the state its handlers share.
@dataclass
class _Daemon:
    server: asyncio.Server
    port: int
    requests: in_flight.RequestTokens
    workers: in_flight.WorkerFutures
    pool: ConnectionPool
    executor: ThreadPoolExecutor


# meiva:note
# The upload receiver of a stack whose tests never receive an upload: its gateway is a closed local
# port, so a request that did reach it would fail loudly, and the delivered-directory name is the
# configured default nobody reads.
def _unreachable_receiver(machine: str, home: Path) -> ReceiverConfig:
    return ReceiverConfig(
        unit=UnitIdentity(machine=machine, home=str(home.resolve())),
        upload_dir_name="uploads",
        access=GatewayAccess(
            gateway_url="http://127.0.0.1:1",
            cluster_secret="local-runner-secret",  # noqa: S106
            remote_plane=False,
            machine_token=None,
            launcher_profile=None,
            gateway_home=False,
        ),
        quota_bytes=MAX_AGENT_UPLOAD_BYTES,
        quota_files=MAX_AGENT_UPLOAD_FILES,
    )


def _receiver_of(machine: str, home: Path, access: GatewayAccess | None) -> ReceiverConfig:
    if access is None:
        return _unreachable_receiver(machine, home)
    return ReceiverConfig(
        unit=UnitIdentity(machine=machine, home=str(home.resolve())),
        upload_dir_name="uploads",
        access=access,
        quota_bytes=MAX_AGENT_UPLOAD_BYTES,
        quota_files=MAX_AGENT_UPLOAD_FILES,
    )


# meiva:note
# Bring up the daemon's serving stack on the running loop; every envelope is recorded on `runner`.
async def _serve(
    runner: RunnerOps,
    *,
    database: Database,
    authority: ConfigAuthority,
    catalog: ModelCatalog,
    machine: MachineIdentityReader,
    host: Host,
    image: LoadedCommit,
    unit_home: Path | None = None,
    gateway_access: GatewayAccess | None = None,
) -> _Daemon:
    requests: in_flight.RequestTokens = set()
    workers: in_flight.WorkerFutures = set()
    pool = boot.open_db_pool(database=lambda: database, authority=authority)
    executor = boot.op_thread_pool(authority=authority)
    home = resolve_meiva_home() if unit_home is None else unit_home.resolve()
    sem = asyncio.Semaphore(authority.service_field_value("ops_concurrency"))
    receiver = _receiver_of(machine.name(), home, gateway_access)
    route = functools.partial(
        ops_route,
        active_ops={},
        workers=workers,
        dispatch_sem=sem,
        requests=requests,
        pool=pool,
        executor=executor,
        catalog=catalog,
        authority=authority,
        database=lambda: database,
        image=image,
        local_machine=machine,
        host=host,
        rng=seeded_rng(),
        home=home,
        receiver=receiver,
        agent_host=ServiceEndpoints.from_fields(get_field, runtime=run_dir(home)).of("agent_host"),
    )
    export_route = functools.partial(
        workspace_export_route,
        pool=pool,
        machine=machine.name(),
        home=home,
        executor=executor,
        dispatch_sem=sem,
        requests=requests,
        workers=workers,
    )
    object_route = functools.partial(
        upload_object_route,
        pool=pool,
        receiver=receiver,
        executor=executor,
        dispatch_sem=sem,
        requests=requests,
        workers=workers,
    )

    async def recorded(body: bytes) -> tuple[int, bytes, str]:
        envelope = json.loads(body)
        runner.calls.append(
            OpsCall(
                kind=envelope["kind"],
                payload=envelope.get("payload", {}),
                idempotency_key=envelope.get("idempotency_key"),
            )
        )
        try:
            return await route(body)
        except BaseException as exc:
            runner.errors.append(exc)
            raise

    server = await start_health_server(
        "ops",
        0,
        home=home if unit_home is not None else meiva_home(),
        extra_routes={
            ("POST", "/ops"): recorded,
            ("POST", "/workspace/export"): export_route,
            ("POST", "/upload-delivery/object"): object_route,
        },
        image=image,
    )
    return _Daemon(
        server=server,
        port=server.sockets[0].getsockname()[1],
        requests=requests,
        workers=workers,
        pool=pool,
        executor=executor,
    )


# meiva:note
# Stop like SIGTERM does: close the listener, give what was admitted the drain bound, then close.
#
# A request or an executor job still running after the drain is a test that left work behind, which
# the deployed daemon would cancel and report; it fails the test that left it.
async def _drain(daemon: _Daemon) -> None:
    daemon.server.close()
    await in_flight.drain(requests=daemon.requests, workers=daemon.workers, timers=SYSTEM_TIMERS)
    await stop_health_server(daemon.server)
    if daemon.requests or daemon.workers:
        raise RuntimeError(
            f"the ops daemon still had {len(daemon.requests)} request(s) and "
            f"{len(daemon.workers)} worker(s) running after the drain"
        )


@contextmanager
def _event_loop() -> Generator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="local-ops-loop")
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


# meiva:note
# Register `machine` as a gateway + agent-runner and answer its ops with the real daemon stack.
@contextmanager
def local_runner(
    conn: psycopg.Connection,
    *,
    database: Database,
    authority: ConfigAuthority,
    catalog: ModelCatalog,
    machine: MachineIdentityReader,
    host: Host,
    image: LoadedCommit,
    unit_home: Path | None = None,
    gateway_access: GatewayAccess | None = None,
) -> Generator[RunnerOps]:
    runner = RunnerOps(name=machine.name(), url="")
    with _event_loop() as loop:
        daemon = asyncio.run_coroutine_threadsafe(
            _serve(
                runner,
                database=database,
                authority=authority,
                catalog=catalog,
                machine=machine,
                host=host,
                image=image,
                unit_home=unit_home,
                gateway_access=gateway_access,
            ),
            loop,
        ).result(timeout=_SETUP_TIMEOUT_S)
        runner.url = f"http://127.0.0.1:{daemon.port}"
        try:
            register_machine(conn, runner.name, roles=("gateway", "agent-runner"), url=runner.url)
            yield runner
        finally:
            try:
                asyncio.run_coroutine_threadsafe(_drain(daemon), loop).result(
                    timeout=_STOP_TIMEOUT_S
                )
            finally:
                daemon.pool.close()
                boot.shutdown_op_pool(daemon.executor)
    if runner.errors:
        raise runner.errors[0]
