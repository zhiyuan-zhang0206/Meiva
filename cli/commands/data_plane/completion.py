# meiva:note
# Completing a gateway's data plane after its migrations: the final grants, the write generation,
# the pooler and the consumer checks.
#
# `instance/storage_plan.py:plan_completion` orders the steps; this module is their shell. It reads
# the few facts the plan needs (is this the home's first start, does its authority ledger exist),
# executes each step in order against the home's own Postgres and stops at the first failure.
# Every function takes the home it works in.

from __future__ import annotations

import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from base import cluster
from base.cluster import authority
from base.cluster.dataplane import pg_admin, pooler
from base.cluster.derive import runner_db_url_projection
from base.cluster.machine import MachineIdentity
from base.cluster.record import ClusterRecord
from base.host.db_authority import delivery as db_delivery_files
from base.host.db_authority import ledger, model
from base.host.db_authority.model import Generation
from base.host.env.dotenv_file import env_file_path, read_env_aliases
from base.host.net.url_secret import url_with_userinfo
from base.native_process.child_env import daemon_process_env
from cli.commands.converge.health_preflight import probe_postgres, probe_redis
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane import pgbouncer
from cli.commands.data_plane.bringup import db_endpoint, prepare_memory_vectors
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.instance.storage_plan import (
    ActivateGeneration,
    AdmitGeneration,
    AdoptLogin,
    BirthGeneration,
    CompletionFacts,
    CompletionStep,
    EnsureExtension,
    EnsurePooler,
    InAdminSession,
    MarkProvisioned,
    PrepareVectors,
    ProbeConsumerDatabases,
    ProbeRedis,
    ProveLogins,
    RefuseLegacy,
    ReportAdmitted,
    legacy_home_refusal,
    plan_completion,
)
from cli.database import OperatorDatabaseFactory
from cli.start_identity import mark_phase, needs_provision

# meiva:note
# Operator read-only roles the authority invariant admits as grantees (SELECT/USAGE/CONNECT only;
# never group members). Names are data: a home without such a role is unaffected.
READONLY_GRANTEES = ("grafana_ro",)


# meiva:note
# The OS-user administrator on this home's owner-only socket (`peer`), autocommit,
# custody-checked against the home's own postmaster.
@contextmanager
def admin_session(
    rec: ClusterRecord, database: str, *, home: Path
) -> Generator[psycopg.Connection[Any]]:
    url = make_conninfo(pg_admin.pg_admin_url(rec.ports["postgres"]), dbname=database)
    data = instance.pg_data_dir(home)
    with pg_admin.connect(url, expected_data_dir=data, autocommit=True) as conn:
        yield conn


# meiva:note
# Serve exactly `generation` through the owned pooler (restart on change).
def ensure_pooler(
    config: DataPlaneConfig,
    rec: ClusterRecord,
    database: str,
    home: Path,
    generation: Generation,
    *,
    machine: MachineIdentity,
) -> None:
    if not config.pgbouncer_enabled:
        return
    spec = pgbouncer.PoolerSpec(
        shape=pgbouncer.PoolerShape(
            pg_port=rec.ports["postgres"],
            listen_port=rec.ports["pgbouncer"],
            db_name=database,
            machine=machine,
            max_client_conn=config.pooler_max_client_conn,
            socket_dir=instance.pg_socket_dir(home),
        ),
        userlist=db_delivery_files.render_userlist(home, generation),
        admin_password=db_delivery_files.read_pooler_admin(home).password,
        binary=pooler.pgbouncer_bin(),
        daemon_env=daemon_process_env(),
        bind_wait_s=instance.BIND_WAIT_TIMEOUT_S,
    )
    rc = pgbouncer.ensure_pgbouncer(spec)
    if rc:
        raise RuntimeError("pooler did not become ready")


# meiva:note
# Both logins of `generation` answer `SELECT 1` through the consumer endpoint (the pooler when
# enabled: SCRAM client auth plus the pass-through hop).
def prove_generation_logins(home: Path, generation: Generation, endpoint: str) -> None:
    secret = ledger.read_secret(home, generation)
    for role in (secret.roles.gateway, secret.roles.runner):
        if error := probe_postgres(url_with_userinfo(endpoint, role.name, role.password)):
            raise RuntimeError(f"write generation {generation.number} login {role.name}: {error}")


# meiva:note
# Point this operator process at the home's active gateway login.
#
# A start that births (or first admits) the generation has no delivery from its own boot, which
# ran before the ledger existed. The login reaches the process through the environment (children
# inherit it) and through the operator's database factory, which every later handle and the
# command's event writer dial through.
def adopt_gateway_login(
    home: Path, endpoint: str, *, database_factory: OperatorDatabaseFactory
) -> None:
    grant = db_delivery_files.write_grant(home, "gateway")
    os.environ["MEIVA_DB_URL"] = grant.dsn(endpoint)
    os.environ[db_delivery_files.GENERATION_ENV] = str(grant.number)
    database_factory.adopt_login(os.environ["MEIVA_DB_URL"])


# meiva:note
# Birth: groups, monitor, ledger, generation 0. The database and its schema owner share one
# identifier.
def _birth_generation(conn: psycopg.Connection[Any], home: Path, database: str) -> Generation:
    owner = database
    groups = model.Groups(gateway=model.GATEWAY_GROUP, runner=model.RUNNER_GROUP)
    authority.ensure_groups(conn, owner=owner, database=database, groups=groups)
    authority.ensure_monitor(conn, database=database)
    ledger.create_ledger(home, owner=owner, groups=groups)
    db_delivery_files.ensure_pooler_admin(home, encrypt=partial(authority.scram_verifier, conn))
    authority.mint_generation(conn, home)
    generation = ledger.require_ledger(home).generation
    if generation is None:
        raise RuntimeError("birth minted no write generation")
    return generation


# meiva:note
# Ordinary start: re-grant after migrations, converge the monitor, then the invariant holds.
def _admitted_generation(
    conn: psycopg.Connection[Any], home: Path, database: str, *, refresh: bool
) -> Generation:
    current = ledger.load_ledger(home)
    if current is None:
        raise RuntimeError(legacy_home_refusal(home))
    if current.owner != database:
        raise RuntimeError(
            f"the database authority ledger records owner {current.owner!r}, but MEIVA_DB_URL "
            f"names database {database!r}"
        )
    if current.active is None:
        raise RuntimeError(
            "the database authority has no active generation; the interrupted birth must "
            "finish before an ordinary start"
        )
    if refresh:
        authority.ensure_groups(conn, owner=current.owner, database=database, groups=current.groups)
        authority.ensure_monitor(conn, database=database)
    authority.check_invariant(conn, home, database=database, readonly_grantees=READONLY_GRANTEES)
    return current.active


# meiva:note
# The state one completion carries from step to step: the home's record and database, the
# endpoint consumers dial, the administrator connection while it is open and the generation the
# birth or admission settled on.
@dataclass
class _Completion:
    config: DataPlaneConfig
    home: Path
    environment: Mapping[str, str]
    database_factory: OperatorDatabaseFactory
    machine: MachineIdentity
    rec: ClusterRecord | None = None
    database: str = ""
    endpoint: str = ""
    conn: psycopg.Connection[Any] | None = None
    generation: Generation | None = None

    def run(self, steps: tuple[CompletionStep, ...]) -> None:
        for step in steps:
            self._execute(step)

    def _record(self) -> ClusterRecord:
        if self.rec is None:
            raise RuntimeError("start lost its port reservation")
        return self.rec

    def _admin(self) -> psycopg.Connection[Any]:
        if self.conn is None:
            raise RuntimeError("the administrator session is not open")
        return self.conn

    def _generation(self) -> Generation:
        if self.generation is None:
            raise RuntimeError("no write generation has been settled")
        return self.generation

    def _runner_url(self) -> str:
        env_file = read_env_aliases(env_file_path(self.home))
        return runner_db_url_projection(self.environment, env_file=env_file)

    def _execute(self, step: CompletionStep) -> None:
        match step:
            case EnsureExtension() | PrepareVectors() | RefuseLegacy():
                self._prepare(step)
            case InAdminSession(steps=inner):
                with admin_session(self._record(), self.database, home=self.home) as conn:
                    self.conn = conn
                    try:
                        self.run(inner)
                    finally:
                        self.conn = None
            case (
                BirthGeneration()
                | AdmitGeneration()
                | EnsurePooler()
                | ProveLogins()
                | ActivateGeneration()
            ):
                self._generation_step(step)
            case AdoptLogin() | ReportAdmitted():
                self._report(step)
            case ProbeConsumerDatabases() | ProbeRedis() | MarkProvisioned():
                self._verify(step)

    def _prepare(self, step: EnsureExtension | PrepareVectors | RefuseLegacy) -> None:
        match step:
            case EnsureExtension():
                cluster.ensure_pgvector_extension(
                    self.database,
                    base_admin_url=pg_admin.pg_admin_url(self._record().ports["postgres"]),
                    expected_data_dir=instance.pg_data_dir(self.home),
                )
            case PrepareVectors():
                prepare_memory_vectors(self.config, database_factory=self.database_factory)
            case RefuseLegacy(message=message):
                raise RuntimeError(message)

    def _generation_step(
        self,
        step: BirthGeneration | AdmitGeneration | EnsurePooler | ProveLogins | ActivateGeneration,
    ) -> None:
        home = self.home.resolve()
        match step:
            case BirthGeneration():
                self.generation = _birth_generation(self._admin(), home, self.database)
            case AdmitGeneration(refresh=refresh):
                self.generation = _admitted_generation(
                    self._admin(), home, self.database, refresh=refresh
                )
            case EnsurePooler():
                ensure_pooler(
                    self.config,
                    self._record(),
                    self.database,
                    home,
                    self._generation(),
                    machine=self.machine,
                )
            case ProveLogins():
                prove_generation_logins(home, self._generation(), self.endpoint)
            case ActivateGeneration():
                if ledger.require_ledger(home).active is None:
                    ledger.activate(home, authority.verify_generation(self._admin(), home))

    def _report(self, step: AdoptLogin | ReportAdmitted) -> None:
        match step:
            case AdoptLogin():
                adopt_gateway_login(
                    self.home.resolve(), self.endpoint, database_factory=self.database_factory
                )
            case ReportAdmitted():
                print(f"  ✓ database write generation {self._generation().number} admitted")

    def _verify(self, step: ProbeConsumerDatabases | ProbeRedis | MarkProvisioned) -> None:
        match step:
            case ProbeConsumerDatabases():
                for url in (self.config.db_url, self._runner_url()):
                    if error := probe_postgres(url):
                        raise RuntimeError(f"consumer database readiness failed: {error}")
            case ProbeRedis():
                if error := probe_redis(self.config.redis_url):
                    raise RuntimeError(f"consumer Redis readiness failed: {error}")
            case MarkProvisioned():
                mark_phase(self.home, "provisioned")


# meiva:note
# Grant the final schema, establish the write generation, start the pooler, then verify consumer
# credentials, in the order `plan_completion` returns.
#
# Local plane, first start (birth, `needs_provision`): groups -> ledger -> mint generation 0 ->
# pooler serving exactly that pair, proven by a pooled login of each -> activate. Every step is
# idempotent, so an interrupted birth retries to the same generation. Ordinary start: groups
# re-granted after migrations, then the fail-closed catalog invariant; the pooler serves the
# active pair (restarted only when its bytes change). A home without a ledger is a legacy home
# and refuses (`legacy_home_refusal`) — never converted here. `environment` is the launch
# environment a remote plane's runner login is projected from.
def complete_gateway_data_plane(
    config: DataPlaneConfig,
    *,
    home: Path,
    environment: Mapping[str, str],
    refresh_schema: bool = True,
    database_factory: OperatorDatabaseFactory,
    machine: MachineIdentity,
) -> None:
    local = not config.is_remote
    facts = CompletionFacts(
        home=home,
        remote=config.is_remote,
        refresh_schema=refresh_schema,
        birth=local and needs_provision(home),
        ledger_present=local and ledger.load_ledger(home.resolve()) is not None,
    )
    run = _Completion(
        config=config,
        home=home,
        environment=environment,
        database_factory=database_factory,
        machine=machine,
    )
    if local:
        run.rec = cluster.get_record(home)
        run.database = cluster.db_identity(config.db_url)
        run.endpoint = db_endpoint(home)
    run.run(plan_completion(facts))
