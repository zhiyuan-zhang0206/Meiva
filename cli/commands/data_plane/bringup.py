# meiva:note
# Gateway data-plane bring-up and remote reachability probes.
#
# The gateway's `meiva start` data-plane step lives here rather than in the start package:
# bring-up chooses between a LOCAL instance (`ensure_cluster_storage` — initdb / pg / redis / ACL
# under the home) and a REMOTE-managed plane (Task #1752) whose URLs name another host, where
# startup degrades to a reachability probe of the URLs themselves and fail-fasts with the dial
# detail. The probes are also used by `meiva status` and `meiva stop` — both must never touch a
# foreign service.
#
# This module is the shell of `instance/storage_plan.py` (what the entry and the completion do and
# in which order), `instance/status_lines.py` and `instance/stop_plan.py` (what status and stop
# print and do) and `instance/launch_delivery.py` (what a launch is delivered). Every function
# takes the home it works in, the pooler's `stop_pgbouncer` included.

from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import assert_never
from urllib.parse import urlsplit

import psutil

from base import cluster
from base.agents.context.clients import DatabaseFactory
from base.clock import Timers
from base.cluster import ownership
from base.cluster import postgres as owned_postgres
from base.cluster.checkpoint_schema import PostgresCheckpointSchema
from base.cluster.dataplane import pg_admin, pooler
from base.cluster.dataplane.pg_tools import pg_tool
from base.cluster.derive import runner_db_url_projection
from base.cluster.machine import MachineIdentityReader
from base.config import ConfigBoot
from base.host.db_authority import delivery as db_delivery_files
from base.host.db_authority import ledger, model
from base.host.db_authority.api import API_TOKEN_ENV, api_token, publish_telemetry_token
from base.host.db_authority.unit import unit_api_delivery, unit_delivery
from base.host.env.bootstrap import config_source_is_local
from base.host.env.dotenv_file import env_file_path, read_env_aliases
from base.host.net.url_secret import url_host
from base.host.proc import run_bounded
from base.host.system.hostport import Host
from base.log import logger
from base.native_process.ownership import OwnedProcess
from base.telemetry import EventPipeline
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import pgbouncer
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.instance import stop_plan
from cli.commands.data_plane.instance.bringup_lines import (
    remote_announce,
    remote_pg_failure,
    remote_ready,
    remote_redis_failure,
)
from cli.commands.data_plane.instance.launch_delivery import (
    Delivery,
    LaunchDelivery,
    api_delivery_kind,
    db_delivery_kind,
)
from cli.commands.data_plane.instance.probe import ProbeState
from cli.commands.data_plane.instance.status_lines import (
    failure_detail,
    local_pg_line,
    local_redis_line,
    pooler_line,
    probe_line,
    remote_status_header,
)
from cli.commands.data_plane.instance.storage_plan import (
    ProbeRemotePlane,
    RecordedPorts,
    Refuse,
    StartLocalStorage,
    admit_gateway_storage,
)
from cli.commands.data_plane.maintenance_stop_report import live_identities
from cli.commands.data_plane.pg_probe import pg_running
from cli.commands.data_plane.stop_waits import (
    PROCESS_CLEANUP_WAIT_S,
    PROCESS_KILL_WAIT_S,
    report_postgres_stop_escalation,
)
from cli.start_identity import needs_provision
from services.supervision.roster.service_spec import DbAccess

_REDIS_SHUTDOWN_TIMEOUT_S = 30.0


# meiva:note
# Bring up owned storage, or probe an explicitly remote-managed plane.
#
# Pooler startup belongs to `complete_gateway_data_plane`, after schema and runner grants.
# Storage ports always come from this home's reservation. The home must carry the registry record
# of its ports; a home born before the data plane always authenticated is refused before any
# native effect, never converted here.
def ensure_gateway_data_plane(
    config: DataPlaneConfig,
    *,
    home: Path,
    host: Host,
    system: str,
    retained_children: list[subprocess.Popen[bytes]] | None,
    database_factory: DatabaseFactory,
    machine: MachineIdentityReader,
    timers: Timers,
    environ: Mapping[str, str],
) -> int:
    if retained_children is None and not config.is_remote:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    rec = cluster.get_record(home)
    admission = admit_gateway_storage(
        home=home,
        ports=None
        if rec is None
        else RecordedPorts(postgres=rec.ports["postgres"], redis=rec.ports["redis"]),
        remote=config.is_remote,
        legacy_home=not config.is_remote and _legacy_home(home),
    )
    match admission:
        case Refuse(message=message):
            print(message, file=sys.stderr)
            return 1
        case ProbeRemotePlane():
            publish_telemetry_token(home.resolve(), config.cluster_secret)
            return _verify_remote_plane(config, database_factory=database_factory)
        case StartLocalStorage(ports=ports):
            if retained_children is None:
                raise RuntimeError("the admitted local storage lost its children")
            publish_telemetry_token(home.resolve(), config.cluster_secret)
            return instance.ensure_cluster_storage(
                config,
                home=home,
                host=host,
                pg_port=ports.postgres,
                redis_port=ports.redis,
                redis_password=cluster.redis_password_from_env(home),
                redis_user=cluster.redis_identity(config.redis_url),
                retained_children=retained_children,
                machine=machine,
                system=system,
                timers=timers,
                environ=environ,
            )
        case _:
            assert_never(admission)


# meiva:note
# Whether the home was born before the data plane always authenticated.
def _legacy_home(home: Path) -> bool:
    return not needs_provision(home) and ledger.load_ledger(home.resolve()) is None


# meiva:note
# Remote-managed data plane (Task #1752): the URLs name another host, so there is no local
# instance to bring up — no initdb, no PgBouncer, no role/ACL provisioning. Unreachable means
# the gateway cannot serve at all, so fail fast with the dial detail instead of a bare psycopg
# traceback from the first migration.
def _verify_remote_plane(config: DataPlaneConfig, *, database_factory: DatabaseFactory) -> int:
    print(remote_announce(url_host(config.db_url)))
    pg_ok, pg_line = remote_pg_reachable(config, database_factory=database_factory)
    if not pg_ok:
        print(remote_pg_failure(pg_line), file=sys.stderr)
        return 1
    redis_ok, redis_line = remote_redis_reachable(config)
    if not redis_ok:
        print(remote_redis_failure(redis_line), file=sys.stderr)
        return 1
    print(remote_ready(pg_line, redis_line))
    return 0


# meiva:note
# Probe a remote-managed Postgres through its own MEIVA_DB_URL.
#
# The two-stage shape of the local probe, minus the local machinery: `base.db.connect()` dials the
# URL as every consumer does (auth included), so an unreachable host and a wrong credential both
# report a real detail line. Bounded by the connect keepalives (5s connect timeout). Returns
# (ok, detail) and never raises.
def remote_pg_reachable(
    config: DataPlaneConfig, *, database_factory: DatabaseFactory
) -> tuple[bool, str]:
    host = url_host(config.db_url)
    port = urlsplit(config.db_url).port or 5432
    try:
        with database_factory().connect() as conn:
            conn.execute("select 1")
        return True, f"postgres ({host}:{port})"
    except Exception as exc:
        return False, f"postgres ({host}:{port}) connect failed: {failure_detail(str(exc))}"


# meiva:note
# Probe a remote-managed Redis through its own MEIVA_REDIS_URL.
#
# PINGs with the URL's own credentials (redis-py `from_url`), unlike the local probe's
# admin-password dial — on a remote/SaaS plane the URL userinfo is the only credential that
# exists. Returns (ok, detail) and never raises.
def remote_redis_reachable(config: DataPlaneConfig) -> tuple[bool, str]:
    import redis as _redis

    host = url_host(config.redis_url)
    port = urlsplit(config.redis_url).port or 6379
    try:
        client = _redis.Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            config.redis_url,
            socket_connect_timeout=3,
        )
        try:
            client.ping()  # pyright: ignore[reportUnknownMemberType]
        finally:
            client.close()
        return True, f"redis ({host}:{port})"
    except Exception as exc:
        return False, f"redis ({host}:{port}) connect failed: {failure_detail(str(exc))}"


# meiva:note
# Whether anything accepts TCP connections on loopback `port`.
def local_listener(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


# meiva:note
# Best-effort warning when a local instance still runs under this home.
#
# After a local→remote data-plane switch (Task #1752), the old local pg/redis are no longer
# managed by this cluster — `meiva stop` skips them, so if they are still running they keep
# consuming the home's ports and data dir. Print a manual-teardown hint instead of silently
# leaving them. Never raises: this is a hint on an already-successful path.
def warn_orphaned_local_instance(home: Path, *, host: Host) -> None:
    try:
        rec = cluster.get_record(home)
        if rec is None:
            return
        postgres = pg_running(
            str(pg_tool("pg_isready", home=home)),
            rec.ports["postgres"],
            "127.0.0.1",
            host=host,
        )
        warning = stop_plan.orphan_warning(
            # meiva:note
            # A probe that could not run says nothing about the server, so it names no leftover:
            # this hint only lists what a probe saw. The registry-driven part of the warning (the
            # Redis listener check below) still runs.
            postgres_running=postgres.state is ProbeState.RUNNING,
            # meiva:note
            # A plain listener check: the left-behind Redis always requires its admin password,
            # which a remote-managed home no longer carries.
            redis_listening=local_listener(rec.ports["redis"]),
        )
        if warning is not None:
            print(warning, file=sys.stderr)
    except Exception:
        logger.opt(exception=True).warning(
            "orphaned-local-instance probe failed; no teardown hint was printed"
        )


# meiva:note
# Initialize only a journal-owned fresh database, before any pooled login.
def prepare_gateway_schema(config: DataPlaneConfig, *, home: Path) -> None:
    if config.is_remote or not needs_provision(home):
        return
    rec = cluster.get_record(home)
    if rec is None:
        raise RuntimeError("initialization lost its port reservation")
    admin_url = pg_admin.pg_admin_url(rec.ports["postgres"], home=home)
    identity = cluster.db_identity(config.db_url)
    data = instance.pg_data_dir(home)
    created = cluster.provision_database(
        identity,
        base_admin_url=admin_url,
        resume_initialization=True,
        expected_data_dir=data,
    )
    cluster.ensure_pgvector_extension(identity, base_admin_url=admin_url, expected_data_dir=data)
    cluster.ensure_checkpoint_schema(
        identity,
        base_admin_url=admin_url,
        database_created=created,
        resume_partial=True,
        expected_data_dir=data,
        schema=PostgresCheckpointSchema(),
    )


# meiva:note
# Create or rebuild the pgvector memory table when that backend is selected.
#
# The table is a derived cache keyed to the embedding provider's dimension. Runtime connections
# only validate it; this start step is its one writer. A local plane writes it acting as the
# schema owner, before the runner grants refresh; a remote-managed plane uses its provider URL,
# like its migrations.
def prepare_memory_vectors(
    config: DataPlaneConfig, *, home: Path, database_factory: DatabaseFactory
) -> None:
    if config.memory_search_backend != "pgvector":
        return
    from services.derived.memory_indexer.backends.pgvector import prepare_table
    from services.derived.memory_indexer.embeddings.factory import get_descriptor

    dim = get_descriptor(config.embedding_backend, revision=config.embedding_revision).dim
    if config.is_remote:
        with database_factory().connect(direct=True) as conn:
            prepare_table(conn, dim)
        return
    owner = pg_admin.local_owner_authority(config.db_url, remote_plane=config.is_remote, home=home)
    with owner.session() as conn:
        prepare_table(conn, dim)


# meiva:note
# The home's credential-free database endpoint, as its `.env` records it.
def db_endpoint(home: Path) -> str:
    endpoint = read_env_aliases(env_file_path(home)).get("MEIVA_DB_URL")
    if not endpoint:
        raise RuntimeError("MEIVA_DB_URL is missing from the gateway configuration")
    return endpoint


# meiva:note
# The facts one service launch reads, taken from the invocation's configuration owner.
#
# Reads the launch environment and the home's `.env` once; `db_delivery`, `api_delivery` and
# `runner_db_url` decide from the result.
def read_launch_delivery(
    *, home: Path, remote_data_plane: bool, config: ConfigBoot
) -> LaunchDelivery:
    return LaunchDelivery(
        home=home,
        remote_data_plane=remote_data_plane,
        environment=dict(config.process_environment()),
        env_file=read_env_aliases(env_file_path(home)),
    )


def _source_is_local(delivery: LaunchDelivery) -> bool:
    return config_source_is_local(delivery.environment, env_file=delivery.env_file)


# meiva:note
# The provider runner login that agents on a remote-managed plane dial: the projection of the
# launch environment and its gateway home's `.env`.
def runner_db_url(delivery: LaunchDelivery) -> str:
    return runner_db_url_projection(delivery.environment, env_file=delivery.env_file)


# meiva:note
# The launch-environment database delivery for one service of class `cls`.
#
# A local plane delivers the active write generation's `cls` login on the home's credential-free
# endpoint plus its non-secret generation number; a missing ledger or active generation fails the
# launch (no fallback). A pure agent-runner delivers its installed unit capability (runner class
# only); without one the launch fails with the issue-unit instruction. A remote-managed gateway
# plane delivers nothing here: its agents keep the provider projection.
def db_delivery(cls: DbAccess, *, delivery: LaunchDelivery) -> dict[str, str]:
    kind = db_delivery_kind(
        cls,
        config_source_is_local=_source_is_local(delivery),
        remote_plane=delivery.remote_data_plane,
    )
    home = delivery.home.resolve()
    match kind:
        case Delivery.UNIT_CAPABILITY:
            return unit_delivery(home)
        case Delivery.NONE:
            return {}
        case Delivery.GENERATION:
            grant = db_delivery_files.write_grant(home, cls)
            return {
                "MEIVA_DB_URL": grant.dsn(db_endpoint(delivery.home)),
                db_delivery_files.GENERATION_ENV: str(grant.number),
            }
        case _:
            assert_never(kind)


# meiva:note
# The launch-environment machine API token for one service of class `cls`.
#
# The cluster's API always authenticates, so every service is delivered a token to present as its
# bearer: a pure agent-runner delivers its capability's runner token; the gateway home delivers
# the active generation's `cls` token. A remote-managed plane keeps no write generations and
# delivers nothing; its gateway-local services present the human secret.
def api_delivery(cls: DbAccess, *, delivery: LaunchDelivery) -> dict[str, str]:
    kind = api_delivery_kind(
        cls,
        config_source_is_local=_source_is_local(delivery),
        remote_plane=delivery.remote_data_plane,
    )
    home = delivery.home.resolve()
    match kind:
        case Delivery.UNIT_CAPABILITY:
            return unit_api_delivery(home)
        case Delivery.NONE:
            return {}
        case Delivery.GENERATION:
            return {API_TOKEN_ENV: api_token(home, cls)}
        case _:
            assert_never(kind)


# meiva:note
# The machine API token a host can issue to a service that runs on every host kind.
#
# `api_delivery` fails the launch when the class it must deliver is not there, which is right
# for a service whose own work needs it. A machine-agnostic service only CARRIES the token into
# the shells it starts, and a host with no data plane of its own — a pure agent-runner with no
# installed capability, a home whose first start never born a write generation — can neither
# issue one nor run an agent. There the launch proceeds with no token rather than refusing a
# service the host otherwise runs (task #5979).
def host_api_delivery(cls: DbAccess, *, delivery: LaunchDelivery) -> dict[str, str]:
    from base.host.db_authority.ledger import ledger_path, load_ledger
    from base.host.db_authority.unit import load_unit_capability

    kind = api_delivery_kind(
        cls,
        config_source_is_local=_source_is_local(delivery),
        remote_plane=delivery.remote_data_plane,
    )
    home = delivery.home.resolve()
    if kind is Delivery.UNIT_CAPABILITY and load_unit_capability(home) is None:
        return {}
    if kind is Delivery.GENERATION and (
        not ledger_path(home).exists() or load_ledger(home) is None
    ):
        return {}
    return api_delivery(cls, delivery=delivery)


# meiva:note
# Print this cluster's own pg/redis reachability for `meiva status`.
#
# Postgres is probed in two stages: `pg_isready` (the server accepts connections) then an
# authenticated `SELECT 1` over the cluster's POOLED front door (`connect()` — PgBouncer when
# enabled, the direct URL when not; the same swap every consumer dials) as this process's
# delivered gateway login. A server that is up but refuses that login reports `✗ ... connect
# failed` rather than a false `✓`: `pg_isready` alone answers without authenticating. Redis pings
# with its admin password. Both ports come from this cluster's own db_url / redis_url (its
# per-cluster instance). The pooler line authenticates to its admin console with the operator
# entry from the home's database authority store.
#
# The probe goes POOLED, never `direct=True` (user ruling 2026-08: every consumer sits behind
# PgBouncer): the pooled `SELECT 1` proves the path consumers actually use — client SCRAM against
# the userlist plus the SCRAM pass-through backend hop. With PgBouncer disabled `pooled_db_url ==
# db_url` and the probe is direct anyway.
def print_data_plane_status(
    config: DataPlaneConfig, *, home: Path, host: Host, database_factory: DatabaseFactory
) -> None:
    if config.is_remote:
        print(remote_status_header())
        pg_ok, pg_line = remote_pg_reachable(config, database_factory=database_factory)
        print(probe_line(ok=pg_ok, detail=pg_line))
        redis_ok, redis_line = remote_redis_reachable(config)
        print(probe_line(ok=redis_ok, detail=redis_line))
        return
    pg_host, redis_host = url_host(config.db_url), url_host(config.redis_url)
    pg_port = urlsplit(config.db_url).port or 5432
    redis_port = urlsplit(config.redis_url).port or 6379
    pg_probe = pg_running(str(pg_tool("pg_isready", home=home)), pg_port, pg_host, host=host)
    connect_error: str | None = None
    if pg_probe.state is ProbeState.RUNNING:
        try:
            # meiva:note
            # pooled front door (PgBouncer when enabled)
            with database_factory().connect() as conn:
                conn.execute("select 1")
        except Exception as exc:
            connect_error = failure_detail(str(exc))
    print(local_pg_line(host=pg_host, port=pg_port, probe=pg_probe, connect_error=connect_error))
    redis_ok = instance.redis_reachable(config.redis_admin_password, redis_port, redis_host)
    print(local_redis_line(host=redis_host, port=redis_port, ok=redis_ok))
    if config.pgbouncer_enabled:
        _print_pooler_status(home)


# meiva:note
# The pooler line: its registry-derived listen port, probed through the admin console as the
# operator entry from the home's authority store.
def _print_pooler_status(home: Path) -> None:
    rec = cluster.get_record(home)
    if rec is None:
        print(pooler_line(port=None, ok=False, refusal=None))
        return
    port = rec.ports["pgbouncer"]
    try:
        admin = db_delivery_files.read_pooler_admin(home)
        ok = pooler.pgbouncer_listener_reachable(port, admin.password)
    except model.AuthorityRefusedError as exc:
        print(pooler_line(port=port, ok=False, refusal=str(exc)))
        return
    print(pooler_line(port=port, ok=ok, refusal=None))


# meiva:note
# Stop this cluster's own Postgres + Redis (data preserved on disk). The counterpart of
# ensure_cluster_storage for `meiva stop` of a cluster running its own instance.
#
# Best-effort: a not-running instance is a no-op success only after native custody closes.
# Returns 0 when everything stopped, 1 after printing why Redis still answers. A remote-managed
# plane has no local instance to stop — the provider owns the service lifecycle — but a cluster
# that switched local to remote may still have its old local instance running, so a hint says so.
def stop_cluster_instance(
    config: DataPlaneConfig,
    *,
    home: Path,
    host: Host,
    macos: bool,
    timers: Timers,
    environ: Mapping[str, str],
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    producer: Callable[[], EventPipeline],
) -> int:
    if config.is_remote:
        print(stop_plan.remote_stop_notice(url_host(config.db_url)))
        warn_orphaned_local_instance(home, host=host)
        return 0
    port = ownership.redis_port_of(config.redis_url)
    # meiva:note
    # Resolve the admin credential before any stop effect: the runtime URL's ACL user can
    # neither AUTH as `default` nor SHUTDOWN (`-@dangerous`).
    redis_env = (
        None
        if port is None
        else instance.redis_cli_env(config.redis_admin_password, environ=environ)
    )
    print("\n→ stopping per-cluster data plane")
    for step in stop_plan.plan_stop(redis_port=port):
        match step:
            case stop_plan.StopPooler():
                # meiva:note
                # Stop the pooler first (best-effort, no-op if it was never enabled) so clients
                # are disconnected before Postgres goes down.
                pgbouncer.stop_pgbouncer(home=home, timers=timers)
            case stop_plan.StopPostgres():
                escalation = owned_postgres.stop(
                    instance.pg_data_dir(home),
                    immediate_wait=PROCESS_CLEANUP_WAIT_S,
                    kill_wait=PROCESS_KILL_WAIT_S,
                    retained_children=retained_children,
                    timers=timers,
                )
                if escalation is not None:
                    report_postgres_stop_escalation(escalation, producer=producer)
                print("  ✓ postgres stopped")
            case stop_plan.StopRedis(port=redis_port):
                if not _stop_redis(config, redis_port, redis_env, home=home, macos=macos):
                    return 1
            case _:
                assert_never(step)
    return 0


# meiva:note
# This home's `redis-server` processes, as native identities.
#
# A `redis-server` counts as this home's when its working directory is the home's Redis data
# directory (the daemon is launched with `--dir` there): the attribution the maintenance stop's
# process scan makes. A process this user cannot inspect is not counted.
def _home_redis_servers(data_dir: Path) -> tuple[OwnedProcess, ...]:
    servers: list[OwnedProcess] = []
    for process in psutil.process_iter(["name"]):
        if process.info["name"] != "redis-server":
            continue
        try:
            if Path(process.cwd()).resolve() == data_dir:
                servers.append(OwnedProcess.capture(process))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return tuple(servers)


# meiva:note
# Shut Redis down as its admin user and verify its port no longer accepts connections: the exit
# status of `shutdown` proves nothing, and neither does an authenticated PING that fails for want
# of the right password, so the check is the listener itself.
#
# Who holds the port decides what the failure says, not whether it is one. The home's Redis
# servers are identified before the shutdown; a listener that outlives it while one of them is
# still alive is that Redis, and a listener with none of them alive is another process on the
# cluster's Redis port. The stop fails either way.
def _stop_redis(
    config: DataPlaneConfig,
    port: int,
    env: dict[str, str] | None,
    *,
    home: Path,
    macos: bool,
) -> bool:
    servers = _home_redis_servers(instance.redis_data_dir(home).resolve())
    returncode, detail = 0, ""
    try:
        shutdown = run_bounded(
            [
                instance.redis_cli_bin(config.redis_bin_dir, macos=macos),
                "-p",
                str(port),
                "shutdown",
                "nosave",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=_REDIS_SHUTDOWN_TIMEOUT_S,
        )
        returncode, detail = shutdown.returncode, (shutdown.stderr or "").strip()[:400]
    except subprocess.TimeoutExpired:
        returncode, detail = -1, "redis-cli shutdown timed out"
    failure = stop_plan.redis_stop_failure(
        port,
        listening=local_listener(port),
        home_redis_alive=bool(live_identities(servers)),
    )
    if failure is not None:
        print(f"  ✗ {failure} (rc={returncode}): {detail}", file=sys.stderr)
        return False
    print("  ✓ redis stopped")
    return True
