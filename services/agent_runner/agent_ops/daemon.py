# meiva:note
# A committed request claim remains after effect failure, cancellation or result-write failure: pending means uncertain and never authorizes re-execution. Duplicate keys validate immutable request identity and wait only for a bounded interval. Only OperationalError is eligible for connection retry. Running workers remain in maintenance accounting after their HTTP waiter is cancelled; repeated stop signals cannot interrupt bounded draining, and default-executor joining cannot prevent process exit. A modeled failed operation remains an HTTP 200 response so callers distinguish its result from transport failure; unexpected result values are string-serialized rather than breaking the control-plane response.
from __future__ import annotations

import argparse
import os
import random
import sys

from base.clock import SYSTEM_TIMERS

if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    from base.log import prepare_process_logging

    prepare_process_logging()


import asyncio
import functools
import logging
import signal
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, assert_never

import psycopg
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base import paths
from base.agents.context.clients import ClientSet
from base.agents.upload_delivery.paths import MAX_AGENT_UPLOAD_BYTES, MAX_AGENT_UPLOAD_FILES
from base.api_contracts.agent_contract import MeivaAgentError
from base.api_contracts.launch_retry import LaunchReconcileRequest
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.transport_encryption import verify_transport_encryption_declaration
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.daemon.health import start_health_server, stop_health_server
from base.daemon.loop_health import LivenessGroup
from base.daemon.shutdown import cancel_and_drain, install_graceful_shutdown
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.db.transaction import write_transaction
from base.host.db_authority.unit import UnitIdentity
from base.host.exit_codes import hard_exit
from base.host.system.hostport import Host
from base.lm.catalog import ModelCatalog
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.plugin_errors import InvalidConfigOverlay
from base.telemetry import build_pipeline
from base.telemetry.process_logging import init_gateway_process
from ops import lifecycle
from ops.cluster_status import ShellNotFoundError
from ops.rpc_schemas import LaunchAgentRequest, LifecyclePayload, OpStatus, is_op_kind
from ops.upload_delivery import ReceiverConfig
from services.agent_runner.agent_ops import boot, health, idempotency_plan, in_flight, ops_plan
from services.agent_runner.agent_ops.dispatch_sync import dispatch_sync
from services.agent_runner.agent_ops.to_user_resource import resource_route
from services.agent_runner.agent_ops.to_user_watch import watch_to_user

_log = logging.getLogger("services.agent_runner.agent_ops.daemon")


DEDUP_WAIT_STEP_S = 0.1
DEDUP_WAIT_ATTEMPTS = 30


DISPATCH_RETRY_ATTEMPTS = 3
DISPATCH_RETRY_BACKOFF_S = 0.5


ActiveOps = dict[str, tuple[str, float]]


# meiva:note
# The process's random generator, built once at its entry for the event bus's retry jitter. Jitter
# is not a secret, so it is a plain `random.Random`.
def _new_rng() -> random.Random:
    return random.Random()  # noqa: S311


async def run_arm(
    kind: str,
    payload: dict[str, Any],
    *,
    active_ops: ActiveOps,
    workers: in_flight.WorkerFutures,
    pool: ConnectionPool,
    executor: ThreadPoolExecutor,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentityReader,
    host: Host,
    rng: random.Random,
    home: Path,
    receiver: ReceiverConfig,
) -> tuple[OpStatus, dict[str, object]]:

    loop = asyncio.get_running_loop()
    active = (kind, time.monotonic())
    active_ops[kind] = active
    try:
        future = loop.run_in_executor(
            executor,
            functools.partial(
                _dispatch_in_thread,
                kind,
                payload,
                pool=pool,
                authority=authority,
                database=database,
                image=image,
                local_machine=local_machine,
                host=host,
                rng=rng,
                home=home,
                receiver=receiver,
            ),
        )
        in_flight.track_worker(future, workers=workers)
        return await asyncio.shield(future)
    finally:
        if active_ops.get(kind) == active:
            active_ops.pop(kind)


def _dispatch_in_thread(
    kind: str,
    payload: dict[str, Any],
    *,
    pool: ConnectionPool,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentityReader,
    host: Host,
    rng: random.Random,
    home: Path,
    receiver: ReceiverConfig,
) -> tuple[OpStatus, dict[str, object]]:

    return dispatch_sync(
        kind,
        payload,
        pool=pool,
        db=boot.ops_handles(database=database, authority=authority, rng=rng)[0],
        authority=authority,
        image=image,
        local_machine=local_machine,
        host=host,
        home=home,
        receiver=receiver,
    )


async def dispatch(
    kind: str,
    payload: dict[str, Any],
    *,
    active_ops: ActiveOps,
    workers: in_flight.WorkerFutures,
    pool: ConnectionPool,
    executor: ThreadPoolExecutor,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentityReader,
    host: Host,
    rng: random.Random,
    home: Path,
    receiver: ReceiverConfig,
) -> tuple[OpStatus, dict[str, object]]:

    if not is_op_kind(kind):
        return ops_plan.unknown_kind(kind)

    try:
        match kind:
            case "spawn-launch":
                db, bus = boot.ops_handles(database=database, authority=authority, rng=rng)
                spawned = await lifecycle.launch_agent_op(
                    db,
                    bus,
                    LaunchAgentRequest.model_validate(payload),
                    pool,
                    catalog=catalog,
                    authority=authority,
                    local_machine=local_machine.name(),
                )

                return OpStatus.COMPLETED, spawned.model_dump(mode="json", exclude_none=True)
            case "launch-reconcile":
                from ops.lifecycle.launch_reconcile import reconcile_launch_op

                db, bus = boot.ops_handles(database=database, authority=authority, rng=rng)
                result = await reconcile_launch_op(
                    db,
                    bus,
                    LaunchReconcileRequest.model_validate(payload),
                    pool,
                    local_machine=local_machine.name(),
                )
                return OpStatus.COMPLETED, result.model_dump(mode="json")
            case "lifecycle":
                lc = LifecyclePayload.model_validate(payload)
                db, bus = boot.ops_handles(database=database, authority=authority, rng=rng)
                resp = await lifecycle.lifecycle_op(
                    db,
                    bus,
                    lc.path,
                    lc.body,
                    pool,
                    trigger_inbound_id=lc.trigger_inbound_id,
                    trigger_inbound_kind=lc.trigger_inbound_kind,
                    catalog=catalog,
                    home=home,
                )
                return OpStatus.COMPLETED, resp.model_dump(mode="json")
            case _:
                return await run_arm(
                    kind,
                    payload,
                    active_ops=active_ops,
                    workers=workers,
                    pool=pool,
                    executor=executor,
                    authority=authority,
                    database=database,
                    image=image,
                    local_machine=local_machine,
                    host=host,
                    rng=rng,
                    home=home,
                    receiver=receiver,
                )
    except (
        MeivaAgentError,
        ValidationError,
        ValueError,
        InvalidConfigOverlay,
        ShellNotFoundError,
    ) as exc:
        return ops_plan.failure_result(exc)


async def dispatch_idempotent(
    kind: str,
    payload: dict[str, Any],
    key: str,
    pool: ConnectionPool,
    *,
    active_ops: ActiveOps,
    workers: in_flight.WorkerFutures,
    executor: ThreadPoolExecutor,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentityReader,
    host: Host,
    rng: random.Random,
    home: Path,
    receiver: ReceiverConfig,
    dedup_wait_attempts: int = DEDUP_WAIT_ATTEMPTS,
    dedup_wait_step_s: float = DEDUP_WAIT_STEP_S,
    retry_backoff_s: float = DISPATCH_RETRY_BACKOFF_S,
) -> tuple[OpStatus, dict[str, object]]:

    if not is_op_kind(kind):
        return ops_plan.unknown_kind(kind)
    failed_attempts = 0
    while True:
        try:
            return await dispatch_idempotent_pass(
                kind,
                payload,
                key,
                pool,
                active_ops=active_ops,
                workers=workers,
                executor=executor,
                catalog=catalog,
                authority=authority,
                database=database,
                image=image,
                local_machine=local_machine,
                host=host,
                rng=rng,
                home=home,
                receiver=receiver,
                dedup_wait_attempts=dedup_wait_attempts,
                dedup_wait_step_s=dedup_wait_step_s,
            )
        except psycopg.OperationalError:
            failed_attempts += 1
            if not idempotency_plan.retry_pass(failed_attempts, DISPATCH_RETRY_ATTEMPTS):
                raise
            _log.warning(
                "dispatch idempotent pass died with OperationalError (attempt %d/%d); retrying",
                failed_attempts,
                DISPATCH_RETRY_ATTEMPTS,
            )
            await asyncio.sleep(retry_backoff_s)


async def dispatch_idempotent_pass(
    kind: str,
    payload: dict[str, Any],
    key: str,
    pool: ConnectionPool,
    *,
    active_ops: ActiveOps,
    workers: in_flight.WorkerFutures,
    executor: ThreadPoolExecutor,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentityReader,
    host: Host,
    rng: random.Random,
    home: Path,
    receiver: ReceiverConfig,
    dedup_wait_attempts: int = DEDUP_WAIT_ATTEMPTS,
    dedup_wait_step_s: float = DEDUP_WAIT_STEP_S,
) -> tuple[OpStatus, dict[str, object]]:

    digest = idempotency_plan.request_hash(kind, payload)
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_idempotency (key, method, path, response_body, request_hash) "
            "VALUES (%s, 'ops', %s, %s, %s) "
            "ON CONFLICT (key) DO NOTHING RETURNING key",
            (key, kind, idempotency_plan.stored_body(payload), digest),
        )
        owned = cur.fetchone() is not None
    if owned:
        status, result = await dispatch(
            kind,
            payload,
            active_ops=active_ops,
            workers=workers,
            pool=pool,
            executor=executor,
            catalog=catalog,
            authority=authority,
            database=database,
            image=image,
            local_machine=local_machine,
            host=host,
            rng=rng,
            home=home,
            receiver=receiver,
        )
        status = OpStatus(status)
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE api_idempotency SET op_status = %s, response_body = %s, "
                "completed_at = now() WHERE key = %s AND method = 'ops'",
                (status, idempotency_plan.stored_body(result), key),
            )
        return status, result

    for _ in range(dedup_wait_attempts):
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT method, path, request_hash, op_status, response_body FROM api_idempotency "
                "WHERE key = %s",
                (key,),
            )
            row = cur.fetchone()
        stored = idempotency_plan.stored_verdict(row, kind, digest)
        match stored:
            case idempotency_plan.Conflict():
                return idempotency_plan.conflict_failure()
            case idempotency_plan.Replay(status=stored_status, result=stored_result):
                return stored_status, stored_result
            case idempotency_plan.Wait():
                await asyncio.sleep(dedup_wait_step_s)
            case _:
                assert_never(stored)
    return idempotency_plan.uncertain_failure(key)


async def ops_route(
    body: bytes,
    *,
    active_ops: ActiveOps,
    workers: in_flight.WorkerFutures,
    dispatch_sem: asyncio.Semaphore,
    requests: in_flight.RequestTokens,
    pool: ConnectionPool,
    executor: ThreadPoolExecutor,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentityReader,
    host: Host,
    rng: random.Random,
    home: Path,
    receiver: ReceiverConfig,
) -> tuple[int, bytes, str]:

    request = ops_plan.read_request(body)
    if isinstance(request, ops_plan.OpReply):
        return request.status_code, request.body, request.content_type
    envelope = request

    # meiva:note
    # Counted from arrival, before the semaphore: a request still queued when SIGTERM drains
    # the server is waited for and, past the budget, cancelled like one that is running.
    with in_flight.admitted(requests=requests):
        async with dispatch_sem:
            try:
                if envelope.idempotency_key is not None:
                    status, result = await dispatch_idempotent(
                        envelope.kind,
                        envelope.payload,
                        envelope.idempotency_key,
                        pool,
                        active_ops=active_ops,
                        workers=workers,
                        executor=executor,
                        catalog=catalog,
                        authority=authority,
                        database=database,
                        image=image,
                        local_machine=local_machine,
                        host=host,
                        rng=rng,
                        home=home,
                        receiver=receiver,
                    )
                else:
                    status, result = await dispatch(
                        envelope.kind,
                        envelope.payload,
                        active_ops=active_ops,
                        workers=workers,
                        pool=pool,
                        executor=executor,
                        catalog=catalog,
                        authority=authority,
                        database=database,
                        image=image,
                        local_machine=local_machine,
                        host=host,
                        rng=rng,
                        home=home,
                        receiver=receiver,
                    )
            except Exception as exc:
                _log.exception("dispatch refused or crashed for kind=%s", envelope.kind)
                status, result = ops_plan.failure_result(exc)
    reply = ops_plan.op_reply(OpStatus(status), result)
    return reply.status_code, reply.body, reply.content_type


# meiva:note
# What the upload receiver needs from its environment, read once by `main()`: this machine and home
# as the unit identity, the OS user's home (where `Downloads/MeivaAgent-N` lives), the gateway
# access and the per-agent quota.
def _receiver_config(config: ConfigBoot, *, machine: str, home: Path) -> ReceiverConfig:
    return ReceiverConfig(
        unit=UnitIdentity(machine=machine, home=str(home.resolve())),
        user_home=Path.home(),
        access=gateway_access_from_boot(config),
        quota_bytes=MAX_AGENT_UPLOAD_BYTES,
        quota_files=MAX_AGENT_UPLOAD_FILES,
    )


async def serve_ops_daemon(
    *,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentityReader,
    config: ConfigBoot,
    host: Host,
    rng: random.Random,
    home: Path,
    receiver: ReceiverConfig,
) -> None:
    from base.deploy.schema.migrations import assert_schema_current

    runtime = config.ensure_eager()
    catalog = build_model_catalog(home=home)
    authority = ConfigAuthority(
        runtime=runtime,
        all_domains=runtime if runtime.profile is None else config.complete_read_model(),
        env_path=home / ".env",
    )

    active_ops: ActiveOps = {}
    requests: in_flight.RequestTokens = set()
    workers: in_flight.WorkerFutures = set()
    runtime_dir = paths.run_dir(home)
    if boot.is_running(config, runtime=runtime_dir):
        _log.info(
            "meiva-ops pidfile %s indicates another instance is alive — exiting",
            boot.pidfile(config, runtime=runtime_dir),
        )
        sys.exit(1)
    boot.write_pidfile(config, runtime=runtime_dir, pid=os.getpid())

    dispatch_sem = asyncio.Semaphore(config.view.services.ops_concurrency)

    our_machine = local_machine.name()

    try:
        assert_schema_current(config.view.data_plane.db_url)
    except Exception as exc:
        _log.error(
            "schema version mismatch on ops-server startup: %s — run `git pull && uv sync && meiva start --all-services` (or this unit's own service selection) to catch up",
            exc,
        )
        boot.remove_pidfile(config, runtime=runtime_dir)
        sys.exit(1)

    pool = boot.open_db_pool(database=database, authority=authority)
    executor = boot.op_thread_pool(authority=authority)
    liveness = LivenessGroup(timers=SYSTEM_TIMERS)

    try:
        acceptance = boot.ops_acceptance(home=home, access=gateway_access_from_boot(config))
        bind_host = boot.ops_bind_host(machine=local_machine)
        if bind_host != "127.0.0.1":
            verify_transport_encryption_declaration(
                bind_host, config.view.data_plane.transport_encryption, authenticated=True
            )
        endpoint = boot.endpoint(config, runtime=runtime_dir)
        server = await start_health_server(
            "ops",
            endpoint.health_port,
            host=bind_host,
            extra_routes={
                ("POST", "/ops"): functools.partial(
                    ops_route,
                    database=database,
                    image=image,
                    executor=executor,
                    catalog=catalog,
                    authority=authority,
                    active_ops=active_ops,
                    dispatch_sem=dispatch_sem,
                    requests=requests,
                    workers=workers,
                    pool=pool,
                    local_machine=local_machine,
                    host=host,
                    rng=rng,
                    home=home,
                    receiver=receiver,
                ),
                ("POST", "/to-user/resource"): functools.partial(
                    resource_route,
                    pool=pool,
                    machine=our_machine,
                    home=home,
                    executor=executor,
                    dispatch_sem=dispatch_sem,
                    requests=requests,
                    workers=workers,
                ),
            },
            liveness=liveness,
            components=lambda: health.ops_components(active_ops),
            extra=lambda: {
                "saturation": health.saturation(
                    active_ops, max(1, config.view.services.ops_concurrency)
                ),
            },
            auth_digests=acceptance,
            image=image,
        )
        _log.info(
            "meiva-ops up, machine=%s serving POST /ops on %s:%d",
            our_machine,
            bind_host,
            endpoint.health_port,
        )
        boot.register_boot(
            database=database,
            config=config,
            machine=local_machine,
            ops_port=endpoint.health_port,
            home=home,
        )
        _, bus = boot.ops_handles(database=database, authority=authority, rng=rng)
        try:
            async with server, asyncio.TaskGroup() as background:
                watcher = background.create_task(
                    watch_to_user(paths.workspaces_root(home=home), bus)
                )
                try:
                    await in_flight.serve_until_sigterm(
                        server, requests=requests, workers=workers, timers=SYSTEM_TIMERS
                    )
                finally:
                    watcher.cancel()
        finally:
            await bus.async_redis().aclose()
            await stop_health_server(server)
    finally:
        pool.close()
        boot.shutdown_op_pool(executor)
        boot.remove_pidfile(config, runtime=runtime_dir)


def main(*, argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    image = LoadedCommit.capture()
    home = paths.meiva_home()
    config = ConfigBoot()
    config.boot()
    config.ensure_eager()
    machine = ConfiguredMachineIdentity(config)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="ops", exempt=False)

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=home),
        )

    clients = ClientSet(
        database=database,
        pipeline_factory=lambda: build_pipeline(
            database=database, machine=machine, home=home.resolve()
        ),
    )
    code = 0
    primary: BaseException | None = None

    try:
        init_gateway_process(
            name="ops",
            producer=clients.event_pipeline,
            machine_reader=machine.name,
            image=image,
            home=home,
        )
        install_graceful_shutdown("ops")
        runner = asyncio.Runner()
        try:
            runner.run(
                serve_ops_daemon(
                    database=database,
                    image=image,
                    local_machine=machine,
                    config=config,
                    host=Host.system(environ=config.process_environment()),
                    rng=_new_rng(),
                    home=home,
                    receiver=_receiver_config(config, machine=machine.name(), home=home),
                )
            )
        except KeyboardInterrupt as interrupted:
            primary = interrupted

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            _log.info("[ops] interrupted, shutting down")

            failures = cancel_and_drain(runner, interrupted_by=interrupted)
            if failures:
                _log.error("[ops] async shutdown failed: %r", failures)
                code = 1
        except Exception as exc:
            primary = exc
            _log.exception("[ops] daemon crashed — uncaught exception escaped serve_ops_daemon()")
            code = 1
    finally:
        failure = primary if primary is not None else sys.exception()
        try:
            clients.close(pipeline_timeout=2)
        except Exception as exc:
            if failure is not None:
                failure.add_note(f"ops event pipeline shutdown failed: {exc!r}")
            _log.exception("[ops] event pipeline shutdown failed")
            code = 1
    hard_exit(code)


if __name__ == "__main__":
    main(argv=[])
