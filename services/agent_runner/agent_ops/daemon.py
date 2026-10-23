# meiva:note
# A committed request claim remains after effect failure, cancellation or result-write failure: pending means uncertain and never authorizes re-execution. Duplicate keys validate immutable request identity and wait only for a bounded interval. Only OperationalError is eligible for connection retry. Running workers remain in maintenance accounting after their HTTP waiter is cancelled; repeated stop signals cannot interrupt bounded draining, and default-executor joining cannot prevent process exit. A modeled failed operation remains an HTTP 200 response so callers distinguish its result from transport failure; unexpected result values are string-serialized rather than breaking the control-plane response.
from __future__ import annotations

import argparse
import sys

from base.clock import SYSTEM_TIMERS

if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    from base.log import prepare_process_logging

    prepare_process_logging()


import asyncio
import functools
import hashlib
import json
import logging
import signal
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base import paths
from base.agents.context.clients import ClientSet
from base.api_contracts.agent_contract import MeivaAgentError
from base.api_contracts.launch_retry import LaunchReconcileRequest
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
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
from ops.rpc_schemas import LaunchAgentRequest, LifecyclePayload, OpEnvelope, OpStatus, is_op_kind
from services.agent_runner.agent_ops import boot, health, in_flight
from services.agent_runner.agent_ops.dispatch_sync import dispatch_sync
from services.agent_runner.agent_ops.to_user_resource import resource_route
from services.agent_runner.agent_ops.to_user_watch import watch_to_user

_log = logging.getLogger("services.agent_runner.agent_ops.daemon")


DEDUP_WAIT_STEP_S = 0.1
DEDUP_WAIT_ATTEMPTS = 30


DISPATCH_RETRY_ATTEMPTS = 3
_DISPATCH_RETRY_BACKOFF_S = 0.5


ActiveOps = dict[str, tuple[str, float]]


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
    local_machine: MachineIdentity,
    host: Host,
    dispatch_arm: Callable[..., Any] | None = None,
) -> tuple[OpStatus, dict[str, object]]:

    arm_fn = bind_dispatch_sync if dispatch_arm is None else dispatch_arm
    loop = asyncio.get_running_loop()
    active = (kind, time.monotonic())
    active_ops[kind] = active
    try:
        future = loop.run_in_executor(
            executor,
            functools.partial(
                arm_fn,
                kind,
                payload,
                pool=pool,
                authority=authority,
                database=database,
                image=image,
                local_machine=local_machine,
                host=host,
            ),
        )
        in_flight.track_worker(future, workers=workers)
        return await asyncio.shield(future)
    finally:
        if active_ops.get(kind) == active:
            active_ops.pop(kind)


def bind_dispatch_sync(
    kind: str,
    payload: dict[str, Any],
    *,
    pool: ConnectionPool,
    authority: ConfigAuthority,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentity,
    host: Host,
    **kwargs: Any,
) -> tuple[OpStatus, dict[str, object]]:

    return dispatch_sync(
        kind,
        payload,
        pool=pool,
        db=boot.ops_handles(database=database, authority=authority)[0],
        authority=authority,
        image=image,
        local_machine=local_machine,
        host=host,
        **kwargs,
    )


async def _reconcile_launch_op(*args: Any, **kwargs: Any) -> Any:
    from ops.lifecycle.launch_reconcile import reconcile_launch_op

    return await reconcile_launch_op(*args, **kwargs)


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
    local_machine: MachineIdentity,
    host: Host,
    dispatch_arm: Callable[..., Any] | None = None,
    launch_agent: Callable[..., Any] = lifecycle.launch_agent_op,
    reconcile_launch: Callable[..., Any] = _reconcile_launch_op,
    run_lifecycle: Callable[..., Any] = lifecycle.lifecycle_op,
) -> tuple[OpStatus, dict[str, object]]:

    if not is_op_kind(kind):
        return OpStatus.FAILED, {"error": f"unknown kind: {kind!r}"}

    try:
        match kind:
            case "spawn-launch":
                db, bus = boot.ops_handles(database=database, authority=authority)
                spawned = await launch_agent(
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
                db, bus = boot.ops_handles(database=database, authority=authority)
                result = await reconcile_launch(
                    db,
                    bus,
                    LaunchReconcileRequest.model_validate(payload),
                    pool,
                    local_machine=local_machine.name(),
                )
                return OpStatus.COMPLETED, result.model_dump(mode="json")
            case "lifecycle":
                lc = LifecyclePayload.model_validate(payload)
                db, bus = boot.ops_handles(database=database, authority=authority)
                resp = await run_lifecycle(
                    db,
                    bus,
                    lc.path,
                    lc.body,
                    pool,
                    trigger_inbound_id=lc.trigger_inbound_id,
                    trigger_inbound_kind=lc.trigger_inbound_kind,
                    catalog=catalog,
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
                    dispatch_arm=dispatch_arm,
                )
    except MeivaAgentError as exc:
        return OpStatus.FAILED, {
            "error": f"{type(exc).__name__}: {exc}",
            "detail": str(exc),
            "reason": exc.reason.value,
        }
    except (ValidationError, ValueError, InvalidConfigOverlay) as exc:
        return OpStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}
    except ShellNotFoundError as exc:
        return OpStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}


_dispatch = dispatch


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
    local_machine: MachineIdentity,
    host: Host,
    dispatch_op: Callable[..., Any] = dispatch,
    sleep: Callable[[float], Any] = asyncio.sleep,
    dedup_wait_attempts: int = DEDUP_WAIT_ATTEMPTS,
    dedup_wait_step_s: float = DEDUP_WAIT_STEP_S,
    run_pass: Callable[..., Any] | None = None,
) -> tuple[OpStatus, dict[str, object]]:

    if not is_op_kind(kind):
        return OpStatus.FAILED, {"error": f"unknown kind: {kind!r}"}
    for attempt in range(DISPATCH_RETRY_ATTEMPTS):
        try:
            if run_pass is not None:
                return await run_pass(
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
                )
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
                dispatch_op=dispatch_op,
                sleep=sleep,
                dedup_wait_attempts=dedup_wait_attempts,
                dedup_wait_step_s=dedup_wait_step_s,
            )
        except psycopg.OperationalError:
            if attempt + 1 >= DISPATCH_RETRY_ATTEMPTS:
                raise
            _log.warning(
                "dispatch idempotent pass died with OperationalError (attempt %d/%d); retrying",
                attempt + 1,
                DISPATCH_RETRY_ATTEMPTS,
            )
            await sleep(_DISPATCH_RETRY_BACKOFF_S)
    raise AssertionError("unreachable")


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
    local_machine: MachineIdentity,
    host: Host,
    dispatch_op: Callable[..., Any] = dispatch,
    sleep: Callable[[float], Any] = asyncio.sleep,
    dedup_wait_attempts: int = DEDUP_WAIT_ATTEMPTS,
    dedup_wait_step_s: float = DEDUP_WAIT_STEP_S,
) -> tuple[OpStatus, dict[str, object]]:

    request_hash = hashlib.sha256(
        json.dumps([kind, payload], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_idempotency (key, method, path, response_body, request_hash) "
            "VALUES (%s, 'ops', %s, %s, %s) "
            "ON CONFLICT (key) DO NOTHING RETURNING key",
            (key, kind, json.dumps(payload), request_hash),
        )
        owned = cur.fetchone() is not None
    if owned:
        status, result = await dispatch_op(
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
        )
        status = OpStatus(status)
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE api_idempotency SET op_status = %s, response_body = %s, "
                "completed_at = now() WHERE key = %s AND method = 'ops'",
                (status, json.dumps(result, default=str), key),
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
        if row is not None:
            if row[0] != "ops" or row[1] != kind or row[2] != request_hash:
                return OpStatus.FAILED, {
                    "error": "idempotency request identity conflict or legacy identity unavailable; "
                    "the stored operation was not re-executed"
                }
            if row[3] is not None:
                result: dict[str, object] = row[4] or {}
                return OpStatus(row[3]), result
        await sleep(dedup_wait_step_s)
    return OpStatus.FAILED, {
        "error": f"idempotency key {key!r} is owned by another dispatch that never "
        "completed; outcome uncertain, inspect the stored operation before recovery"
    }


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
    local_machine: MachineIdentity,
    host: Host,
    dispatch_op: Callable[..., Any] = dispatch,
) -> tuple[int, bytes, str]:

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        return 400, json.dumps({"error": f"invalid JSON body: {exc}"}).encode(), "application/json"
    try:
        envelope = OpEnvelope.model_validate(parsed)
    except ValidationError as exc:
        return (
            400,
            json.dumps({"error": f"body must be {{kind: str, payload: dict}}: {exc}"}).encode(),
            "application/json",
        )

    if not is_op_kind(envelope.kind):
        return (
            200,
            json.dumps(
                {"status": OpStatus.FAILED, "result": {"error": f"unknown kind: {envelope.kind!r}"}}
            ).encode(),
            "application/json",
        )

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
                        dispatch_op=dispatch_op,
                    )
                else:
                    status, result = await dispatch_op(
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
                    )
            except Exception as exc:
                _log.exception("dispatch refused or crashed for kind=%s", envelope.kind)
                status, result = OpStatus.FAILED, {"error": f"{type(exc).__name__}: {exc}"}
    status = OpStatus(status)

    return (
        200,
        json.dumps({"status": status, "result": result}, default=str).encode(),
        "application/json",
    )


def _assert_schema_current(db_url: str) -> None:
    from base.deploy.schema.migrations import assert_schema_current

    assert_schema_current(db_url)


async def serve_ops_daemon(
    *,
    database: Callable[[], Database],
    image: LoadedCommit,
    local_machine: MachineIdentity,
    config: ConfigBoot,
    host: Host,
    is_running: Callable[..., bool] = boot.is_running,
    write_pidfile: Callable[..., None] = boot.write_pidfile,
    remove_pidfile: Callable[..., None] = boot.remove_pidfile,
    check_schema_current: Callable[[str], None] = _assert_schema_current,
    open_db_pool: Callable[..., Any] = boot.open_db_pool,
    ops_acceptance: Callable[[], frozenset[str]] = boot.ops_acceptance,
    ops_bind_host: Callable[..., str] = boot.ops_bind_host,
    register_boot: Callable[..., None] = boot.register_boot,
    start_health: Callable[..., Any] = start_health_server,
    stop_health: Callable[..., Any] = stop_health_server,
) -> None:
    runtime = config.ensure_eager()
    catalog = build_model_catalog()
    authority = ConfigAuthority(
        runtime=runtime,
        all_domains=runtime if runtime.profile is None else config.complete_read_model(),
        env_path=paths.meiva_home() / ".env",
    )

    active_ops: ActiveOps = {}
    requests: in_flight.RequestTokens = set()
    workers: in_flight.WorkerFutures = set()
    runtime_dir = paths.run_dir(paths.meiva_home())
    if is_running(config, runtime=runtime_dir):
        _log.info(
            "meiva-ops pidfile %s indicates another instance is alive — exiting",
            boot.pidfile(config, runtime=runtime_dir),
        )
        sys.exit(1)
    write_pidfile(config, runtime=runtime_dir)

    dispatch_sem = asyncio.Semaphore(config.view.services.ops_concurrency)

    our_machine = local_machine.name()

    try:
        check_schema_current(config.view.data_plane.db_url)
    except Exception as exc:
        _log.error(
            "schema version mismatch on ops-server startup: %s — run `git pull && uv sync && meiva start --all-services` (or this unit's own service selection) to catch up",
            exc,
        )
        sys.exit(1)

    pool = open_db_pool(database=database, authority=authority)
    executor = boot.op_thread_pool(authority=authority)
    liveness = LivenessGroup(timers=SYSTEM_TIMERS)

    try:
        acceptance = ops_acceptance()
        bind_host = ops_bind_host(machine=local_machine)
        if bind_host != "127.0.0.1":
            verify_transport_encryption_declaration(
                bind_host, config.view.data_plane.transport_encryption, authenticated=True
            )
        endpoint = boot.endpoint(config, runtime=runtime_dir)
        server = await start_health(
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
                ),
                ("POST", "/to-user/resource"): functools.partial(
                    resource_route,
                    pool=pool,
                    machine=our_machine,
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
        register_boot(
            database=database,
            config=config,
            machine=local_machine,
            ops_port=endpoint.health_port,
            home=paths.meiva_home(),
        )
        _, bus = boot.ops_handles(database=database, authority=authority)
        try:
            async with server, asyncio.TaskGroup() as background:
                watcher = background.create_task(watch_to_user(paths.workspaces_root(), bus))
                try:
                    await in_flight.serve_until_sigterm(server, requests=requests, workers=workers)
                finally:
                    watcher.cancel()
        finally:
            await bus.async_redis().aclose()
            await stop_health(server)
            remove_pidfile(config, runtime=runtime_dir)
    finally:
        pool.close()
        boot.shutdown_op_pool(executor)


def main(
    *,
    argv: list[str] | None = None,
    exit_process: Callable[[int], None] = hard_exit,
    capture_image: Callable[[], LoadedCommit] = LoadedCommit.capture,
    config_factory: Callable[[], ConfigBoot] = ConfigBoot,
    database_cls: Callable[..., Database] = Database,
    client_set_cls: Callable[..., Any] = ClientSet,
    pipeline_builder: Callable[..., Any] = build_pipeline,
    init_process: Callable[..., None] = init_gateway_process,
    install_shutdown: Callable[[str], None] = install_graceful_shutdown,
    runner_factory: Callable[[], asyncio.Runner] = asyncio.Runner,
    serve_daemon: Callable[..., Any] = serve_ops_daemon,
) -> None:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    image = capture_image()
    config = config_factory()
    config.boot()
    config.ensure_eager()
    machine = machine_from_boot(config)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="ops", exempt=False)

    def database() -> Database:
        return database_cls(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    clients = client_set_cls(
        database=database,
        pipeline_factory=lambda: pipeline_builder(
            database=database, machine=machine, home=paths.meiva_home().resolve()
        ),
    )
    code = 0
    primary: BaseException | None = None

    try:
        init_process(
            name="ops", producer=clients.event_pipeline, machine_reader=machine.name, image=image
        )
        install_shutdown("ops")
        runner = runner_factory()
        try:
            runner.run(
                serve_daemon(
                    database=database,
                    image=image,
                    local_machine=machine,
                    config=config,
                    host=Host.system(),
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
    exit_process(code)


if __name__ == "__main__":
    main(argv=[])
