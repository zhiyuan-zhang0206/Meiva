"""Single-box birth and ordinary start on the always-authenticated data plane.

Real Postgres 17, PgBouncer and Redis under a temp `$MEIVA_HOME`, driven through
the official start steps (`ensure_gateway_data_plane` -> `prepare_gateway_schema`
-> `cmd_migrations_apply` -> `complete_gateway_data_plane`) with an EMPTY
cluster secret — the single-box default that used to mean "no credentials".

A fresh birth must leave: a NOLOGIN, password-less schema owner; the two
capability groups; write generation 0 active in the private ledger; a pooler
serving exactly that pair with SCRAM (plus the admin console entry); and no
passwordless door for anything but the OS user over the owner-only socket.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import psycopg
import pytest
from redis.exceptions import RedisError

from base import cluster
from base.cluster import authority, ownership
from base.cluster.dataplane import pooler as base_pooler
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.dataplane.pg_tools import pg_tool
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.host.db_authority import delivery, ledger, model
from base.host.db_authority.api import API_TOKEN_ENV
from base.host.env import port_lease
from base.host.env.registry import process_environment
from base.host.net.url_secret import url_with_userinfo
from base.native_process.child_env import daemon_process_env
from cli.commands.data_plane import bringup, completion
from cli.commands.data_plane import cluster_instance as ci
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.instance.launch_delivery import LaunchDelivery
from cli.commands.data_plane.instance.storage_plan import (
    BirthGeneration,
    EnsurePooler,
    InAdminSession,
)
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.commands.data_plane.tests.native_cluster import shutdown_redis
from cli.commands.lifecycle.start.migrations import cmd_migrations_apply
from cli.database import (
    OperatorDatabaseFactory,
    open_operator_database,
    operator_database_factory,
)
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.backup.artifact import passphrase

pytestmark = pytest.mark.skipif(
    not (Path(base_pooler.pgbouncer_bin()).exists() or shutil.which(base_pooler.pgbouncer_bin())),
    reason="pgbouncer not installed (brew/apt)",
)

_REPO = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Born:
    home: Path
    record: cluster.ClusterRecord
    values: dict[str, str]
    plane: DataPlaneConfig
    retained_children: list[subprocess.Popen[bytes]] = field(default_factory=list)

    @property
    def pg_port(self) -> int:
        return self.record.ports["postgres"]

    @property
    def pooler_port(self) -> int:
        return self.record.ports["pgbouncer"]

    def endpoint(self) -> str:
        return self.values["MEIVA_DB_URL"]

    def login(self, cls: str) -> tuple[str, str]:
        secret = ledger.read_secret(self.home, delivery.active_generation(self.home))
        role = secret.roles.of(cast("Any", cls))
        return role.name, role.password

    def dsn(self, cls: str) -> str:
        name, password = self.login(cls)
        return url_with_userinfo(self.endpoint(), name, password)

    def admin(self) -> psycopg.Connection[Any]:
        return psycopg.connect(
            host=str(ci.pg_socket_dir(self.home)),
            port=self.pg_port,
            dbname="meiva",
            autocommit=True,
        )


def _intent(home: Path, record: cluster.ClusterRecord, values: dict[str, str]) -> None:
    body = {
        "version": 1,
        "home": str(home),
        "checkout": str(_REPO),
        "roles": ["agent-runner", "gateway"],
        "config_digest": None,
        "phase": "configured",
        "record": asdict(record),
        "env": values,
    }
    path = home / "start-intent.json"
    path.write_text(json.dumps(body, sort_keys=True) + "\n")
    path.chmod(0o600)


def _teardown(born: Born) -> None:
    try:
        pooler.stop_pgbouncer(force=True)
    finally:
        subprocess.run(
            [str(pg_tool("pg_ctl")), "-D", str(born.home / "pg"), "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
        for child in born.retained_children:
            child.wait(timeout=1)
        with contextlib.suppress(RedisError):
            shutdown_redis(
                born.record.ports["redis"], born.values["MEIVA_REDIS_ADMIN_PASSWORD"], save=False
            )


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    distinct_free_ports: Callable[[int], list[int]],
    unit_plane: DataPlaneConfig,
) -> Born:
    """A configured-but-unprovisioned single-box home (minted cluster secret)."""
    from cli.start_identity import IdentityInput, gateway_values

    home = (tmp_path / "home").resolve()
    home.mkdir(mode=0o700)
    monkeypatch.setenv("MEIVA_HOME", str(home))
    # meiva:note
    # Allocate the entire roster at once. The lease hands out a number it never repeats
    # while this session lives, so the slots cannot collide with each other, with another
    # holder on the host, or with a later fixture that asks for one more port.
    ports = dict(
        zip(cluster.FIXED_PORTS, distinct_free_ports(len(cluster.FIXED_PORTS)), strict=True)
    )
    record = cluster.ClusterRecord(
        ports=cast("cluster.ClusterPorts", ports), gateway_home=str(home), created_at="test"
    )
    values = gateway_values(
        record,
        IdentityInput(
            home,
            _REPO,
            frozenset({"gateway", "agent-runner"}),
            {},
            occupied_ports=frozenset(),
        ),
    )
    assert values["MEIVA_CLUSTER_SECRET"]
    assert "MEIVA_DB_ADMIN_PASSWORD" not in values
    assert "MEIVA_RUNNER_DB_PASSWORD" not in values
    (home / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    _intent(home, record, values)
    passphrase.ensure_minted(home)  # as the birth's claim publication pins it
    # meiva:note
    # The config the commands read: the unit environment's values with the minted ones.
    plane = replace(
        unit_plane,
        cluster_secret=values["MEIVA_CLUSTER_SECRET"],
        db_url=values["MEIVA_DB_URL"],
        redis_url=values["MEIVA_REDIS_URL"],
        redis_admin_password=values["MEIVA_REDIS_ADMIN_PASSWORD"],
        events_channel=values["MEIVA_EVENTS_CHANNEL"],
        pgbouncer_enabled=True,
    )
    # The start process adopts its delivered login into os.environ; the raw-env
    # seam restores the suite's own values at teardown.
    monkeypatch.setitem(os.environ, "MEIVA_DB_URL", values["MEIVA_DB_URL"])
    # The birth adopts the delivered generation next. `delitem(raising=False)` on an absent
    # key records nothing, so register the key (setitem) before removing it: teardown then
    # deletes whatever the birth assigned.
    monkeypatch.setitem(os.environ, delivery.GENERATION_ENV, "")
    monkeypatch.delitem(os.environ, delivery.GENERATION_ENV)

    return Born(home=home, record=record, values=values, plane=plane)


# meiva:note
# Birth records this session's leased roster, not whatever a kernel probe would hand out.
#
# This test used to fake `socket.socket` with a kernel that releases a port for reuse:
# that is how two closed probes could be handed one number and duplicate a slot. A leased
# number belongs to the session for its whole life (`base/host/env/tests/test_port_lease.py`
# proves that across processes), so the roster is distinct by construction — and the birth
# still holds no socket of its own.
def test_configured_roster_is_this_sessions_leased_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    distinct_free_ports: Callable[[int], list[int]],
    data_plane_config: DataPlaneConfig,
) -> None:
    from cli.start_identity import read_intent

    configured = _configure(monkeypatch, tmp_path, distinct_free_ports, data_plane_config)
    intent = read_intent(configured.home)
    assert intent is not None
    assert set(configured.record.ports) == set(cluster.FIXED_PORTS)
    assert len(set(configured.record.ports.values())) == len(cluster.FIXED_PORTS)
    assert set(configured.record.ports.values()) <= set(port_lease.leased_ports())


def _prepare_schema(
    born: Born, *, operator_database: OperatorDatabaseFactory, command_runtime: CommandRuntime
) -> None:
    assert (
        bringup.ensure_gateway_data_plane(
            born.plane,
            home=born.home,
            retained_children=born.retained_children,
            database_factory=operator_database,
            machine=command_runtime.machine,
            macos=command_runtime.host.platform.system == "darwin",
        )
        == 0
    )
    bringup.prepare_gateway_schema(born.plane, home=born.home)
    cmd_migrations_apply(
        database_factory=operator_database,
        machine=command_runtime.machine,
        config=born.plane,
        home=born.home,
    )


def _birth(
    born: Born, *, operator_database: OperatorDatabaseFactory, command_runtime: CommandRuntime
) -> None:
    _prepare_schema(born, operator_database=operator_database, command_runtime=command_runtime)
    completion.complete_gateway_data_plane(
        born.plane,
        home=born.home,
        environment=os.environ,
        database_factory=operator_database,
        machine=command_runtime.machine,
        macos=command_runtime.host.platform.system == "darwin",
    )


@pytest.fixture
def configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_access: None,
    redis_access: None,
    distinct_free_ports: Callable[[int], list[int]],
    data_plane_config: DataPlaneConfig,
) -> Iterator[Born]:
    # These commands simulate a process entry and deliver its unit environment.
    with patch.dict(os.environ):
        born = _configure(monkeypatch, tmp_path, distinct_free_ports, data_plane_config)
        try:
            yield born
        finally:
            _teardown(born)


@pytest.fixture
def born(
    configured: Born, *, operator_database: OperatorDatabaseFactory, command_runtime: CommandRuntime
) -> Born:
    _birth(configured, operator_database=operator_database, command_runtime=command_runtime)
    return configured


# A login without a stored verifier fails authentication before PostgreSQL
# even reaches its LOGIN check; PgBouncer reports its own SCRAM failure.
_AUTH_REFUSED = (
    "password authentication failed",
    "not permitted to log in",
    "SASL authentication failed",
    "no password supplied",
)


def assert_refused(**kwargs: Any) -> str:
    with pytest.raises(psycopg.OperationalError) as excinfo:
        psycopg.connect(connect_timeout=5, **kwargs).close()
    message = str(excinfo.value)
    assert any(marker in message for marker in _AUTH_REFUSED), message
    return message


def test_fresh_birth_mints_generation_zero_behind_nologin_owner(
    born: Born, operator_database: OperatorDatabaseFactory
) -> None:
    current = ledger.require_ledger(born.home)
    assert current.owner == "meiva"
    assert current.active is not None and current.active.number == 0
    assert current.active.origin.kind == "birth"
    with born.admin() as conn:
        owner = conn.execute(
            "SELECT rolcanlogin, rolpassword IS NULL, rolsuper FROM pg_authid WHERE rolname = 'meiva'"
        ).fetchone()
        assert owner == (False, True, False)
        groups = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname IN ('meiva_gateway', 'meiva_runner')"
            " AND NOT rolcanlogin ORDER BY 1"
        ).fetchall()
        assert groups == [("meiva_gateway",), ("meiva_runner",)]
        authority.check_invariant(
            conn, born.home, database="meiva", readonly_grantees=completion.READONLY_GRANTEES
        )
    # The start process adopted the delivered gateway login for its own dials.
    assert operator_database.db_url() == born.dsn("gateway")
    assert os.environ[delivery.GENERATION_ENV] == "0"
    assert json.loads((born.home / "start-intent.json").read_text())["phase"] == "provisioned"


def test_birth_refuses_unauthenticated_and_owner_logins(born: Born) -> None:
    socket_dir = str(ci.pg_socket_dir(born.home))
    for host in ("127.0.0.1", socket_dir):
        # The owner exists but can never log in, with or without a password.
        for password in ("", "anything"):
            assert_refused(
                host=host, port=born.pg_port, user="meiva", password=password, dbname="meiva"
            )
    # No password-less door over TCP, even for the OS-user superuser.
    assert_refused(
        host="127.0.0.1", port=born.pg_port, user=getpass.getuser(), password="", dbname="meiva"
    )
    # The pooler admits neither the owner nor a login without its password.
    assert_refused(
        host="127.0.0.1",
        port=born.pooler_port,
        user="meiva",
        password="x",  # noqa: S106 — a wrong credential by design
        dbname="meiva",
    )
    name, _password = born.login("gateway")
    assert_refused(
        host="127.0.0.1",
        port=born.pooler_port,
        user=name,
        password="wrong",  # noqa: S106 — a wrong credential by design
        dbname="meiva",
    )
    # The OS user reaches the administrator only through peer on the owner-only socket.
    with born.admin() as conn:
        assert conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = session_user"
        ).fetchone() == (True,)


def test_generation_logins_work_with_group_privileges(born: Born) -> None:
    with psycopg.connect(born.dsn("gateway"), prepare_threshold=None, autocommit=True) as gw:
        assert gw.execute("SELECT session_user").fetchone() == (born.login("gateway")[0],)
        gw.execute("INSERT INTO agents (id) VALUES (910001)")
        gw.execute("INSERT INTO agents_meta (id, status) VALUES (910001, 'running')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gw.execute("CREATE TABLE gateway_must_not_create (id int)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            gw.execute("SET ROLE meiva")
    with psycopg.connect(born.dsn("runner"), prepare_threshold=None, autocommit=True) as runner:
        assert runner.execute("SELECT count(*) FROM agents WHERE id = 910001").fetchone() == (1,)
        runner.execute("UPDATE agents_meta SET status = 'idling' WHERE id = 910001")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runner.execute("INSERT INTO agents (id) VALUES (910002)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runner.execute("SET ROLE meiva_gateway")


def test_pooler_admin_console_is_the_userlist_only_operator_entry(born: Born) -> None:
    admin = delivery.read_pooler_admin(born.home)
    assert base_pooler.pgbouncer_listener_reachable(born.pooler_port, admin.password)
    assert not base_pooler.pgbouncer_listener_reachable(born.pooler_port, "wrong-password-xxxxx")
    userlist = (born.home / "pgbouncer" / "userlist.txt").read_bytes()
    assert userlist == delivery.render_userlist(born.home, delivery.active_generation(born.home))
    names = [line.split('"')[1] for line in userlist.decode().splitlines()]
    assert names == [*born.login("gateway")[:1], *born.login("runner")[:1], delivery.POOLER_ADMIN]
    assert b"SCRAM-SHA-256$" in userlist
    assert admin.password.encode() not in userlist


def test_pooler_restart_revokes_a_removed_user_a_reload_would_keep(
    born: Born, *, command_runtime: CommandRuntime
) -> None:
    """E11: after a SIGHUP PgBouncer still admits a removed user that had
    authenticated; changed userlist bytes therefore restart the pooler."""
    name, password = born.login("gateway")
    with psycopg.connect(born.dsn("gateway"), prepare_threshold=None) as conn:
        conn.execute("SELECT 1")
    owner_before = ownership.pooler(base_pooler.ini_path(), base_pooler.pidfile_path())
    assert owner_before is not None
    admin = delivery.read_pooler_admin(born.home)
    userlist = delivery.render_userlist(born.home, delivery.active_generation(born.home))
    without_gateway = b"".join(
        line
        for line in userlist.splitlines(keepends=True)
        if not line.startswith(f'"{name}"'.encode())
    )
    spec = pooler.PoolerSpec(
        shape=pooler.PoolerShape(
            pg_port=born.pg_port,
            listen_port=born.pooler_port,
            db_name="meiva",
            machine=command_runtime.machine,
            max_client_conn=2000,
            socket_dir=ci.pg_socket_dir(born.home),
        ),
        userlist=without_gateway,
        admin_password=admin.password,
        binary=base_pooler.pgbouncer_bin(),
        daemon_env=daemon_process_env(),
        macos=command_runtime.host.platform.system == "darwin",
        bind_wait_s=ci.BIND_WAIT_TIMEOUT_S,
    )
    rc = pooler.ensure_pgbouncer(spec)
    assert rc == 0
    owner_after = ownership.pooler(base_pooler.ini_path(), base_pooler.pidfile_path())
    assert owner_after is not None and owner_after.pid != owner_before.pid
    assert_refused(
        host="127.0.0.1", port=born.pooler_port, user=name, password=password, dbname="meiva"
    )


def test_unchanged_userlist_reloads_without_restart(
    born: Born,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    before = ownership.pooler(base_pooler.ini_path(), base_pooler.pidfile_path())
    completion.complete_gateway_data_plane(
        born.plane,
        home=born.home,
        environment=os.environ,
        database_factory=operator_database,
        machine=command_runtime.machine,
        macos=command_runtime.host.platform.system == "darwin",
    )
    after = ownership.pooler(base_pooler.ini_path(), base_pooler.pidfile_path())
    assert before is not None and after is not None and before.pid == after.pid


def test_ordinary_start_refuses_a_stray_group_member_and_leaves_it_untouched(
    born: Born,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    with born.admin() as conn:
        conn.execute(
            "CREATE ROLE meiva_g7_runner LOGIN PASSWORD 'stale-password-xyz' IN ROLE meiva_runner"
        )
    with pytest.raises(model.CatalogRefusedError, match="meiva_g7_runner"):
        completion.complete_gateway_data_plane(
            born.plane,
            home=born.home,
            environment=os.environ,
            database_factory=operator_database,
            machine=command_runtime.machine,
            macos=command_runtime.host.platform.system == "darwin",
        )
    with born.admin() as conn:
        assert conn.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'meiva_g7_runner'"
        ).fetchone() == (True,)
    assert delivery.active_generation(born.home).number == 0


def test_ordinary_start_holds_on_an_invariant_violation(
    born: Born,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    with born.admin() as conn:
        conn.execute("CREATE ROLE foreign_writer LOGIN PASSWORD 'foreign-password'")
        conn.execute("GRANT INSERT ON agents TO foreign_writer")
    with pytest.raises(model.CatalogRefusedError, match="foreign_writer"):
        completion.complete_gateway_data_plane(
            born.plane,
            home=born.home,
            environment=os.environ,
            database_factory=operator_database,
            machine=command_runtime.machine,
            macos=command_runtime.host.platform.system == "darwin",
        )


def test_ordinary_start_refuses_a_home_without_a_ledger_before_any_effect(
    configured: Born,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    intent = json.loads((configured.home / "start-intent.json").read_text())
    intent["phase"] = "provisioned"
    (configured.home / "start-intent.json").write_text(json.dumps(intent))
    assert (
        bringup.ensure_gateway_data_plane(
            configured.plane,
            home=configured.home,
            retained_children=configured.retained_children,
            database_factory=operator_database,
            machine=command_runtime.machine,
            macos=command_runtime.host.platform.system == "darwin",
        )
        == 1
    )
    assert "no conversion exists" in capsys.readouterr().err
    assert not (configured.home / "pg").exists()
    with pytest.raises(RuntimeError, match="no database authority ledger"):
        completion.complete_gateway_data_plane(
            configured.plane,
            home=configured.home,
            environment=os.environ,
            database_factory=operator_database,
            machine=command_runtime.machine,
            macos=command_runtime.host.platform.system == "darwin",
        )


# meiva:note
# A birth that stops after the pooler is up and before its logins are proven leaves generation 0
# pending, and the next start settles on that same generation. The first start is the completion
# plan cut before `ProveLogins`, run as it is by `execute_completion`.
def test_interrupted_birth_retries_to_the_same_generation(
    configured: Born,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    macos = command_runtime.host.platform.system == "darwin"
    _prepare_schema(
        configured, operator_database=operator_database, command_runtime=command_runtime
    )

    completion.execute_completion(
        (InAdminSession((BirthGeneration(), EnsurePooler())),),
        configured.plane,
        home=configured.home,
        environment=os.environ,
        database_factory=operator_database,
        machine=command_runtime.machine,
        macos=macos,
    )

    interrupted = ledger.require_ledger(configured.home)
    assert interrupted.active is None
    assert interrupted.pending is not None and interrupted.pending.number == 0
    completion.complete_gateway_data_plane(
        configured.plane,
        home=configured.home,
        environment=os.environ,
        database_factory=operator_database,
        machine=command_runtime.machine,
        macos=macos,
    )
    settled = ledger.require_ledger(configured.home)
    assert settled.active is not None and settled.active.number == 0


def test_launched_services_receive_their_class_login_only(born: Born) -> None:
    from base.sessions.env_forwarding import managed_service_env
    from cli.commands.lifecycle.root_driver import service_extra_env
    from services.supervision.roster.service_spec import ServiceSpec

    # The launcher's settings-free serve-gateway read (a born single box).
    launch = LaunchDelivery(
        home=born.home,
        remote_data_plane=False,
        environment={"MEIVA_MACHINE_SERVE_GATEWAY": "true"},
        env_file={},
    )

    gateway = ServiceSpec(
        session="gateway", cmd="x", capabilities=frozenset({"gateway"}), requires_db=True
    )
    runner = ServiceSpec(
        session="ops", cmd="x", capabilities=frozenset({"agent-runner"}), requires_db=True
    )
    frontend = ServiceSpec(
        session="frontend", cmd="x", capabilities=frozenset({"gateway"}), requires_db=False
    )
    assert service_extra_env(gateway, delivery=launch)["MEIVA_DB_URL"] == born.dsn("gateway")
    assert service_extra_env(runner, delivery=launch)["MEIVA_DB_URL"] == born.dsn("runner")
    assert service_extra_env(runner, delivery=launch)[delivery.GENERATION_ENV] == "0"
    assert "MEIVA_DB_URL" not in service_extra_env(frontend, delivery=launch)
    # The born single box authenticates its API: each service is delivered its class
    # token, the database-free frontend included, and never the root itself.
    tokens = ledger.read_secret(born.home, delivery.active_generation(born.home)).api
    assert service_extra_env(gateway, delivery=launch)[API_TOKEN_ENV] == tokens.gateway
    assert service_extra_env(runner, delivery=launch)[API_TOKEN_ENV] == tokens.runner
    assert service_extra_env(frontend, delivery=launch)[API_TOKEN_ENV] == tokens.gateway
    root = managed_service_env(settings.general.service_path, environ=process_environment())
    assert "MEIVA_DB_URL" not in root and delivery.GENERATION_ENV not in root
    assert API_TOKEN_ENV not in root


_ROLES = frozenset({"gateway", "agent-runner"})


def _collector_postgres_receiver(
    born: Born, *, operator_database: OperatorDatabaseFactory, command_runtime: CommandRuntime
) -> dict[str, Any]:
    """The rendered collector config's PostgreSQL receiver; the whole rendered
    text carries no credential of the data plane."""
    import yaml

    from cli.commands.observability import otel_collector as oc
    from cli.commands.observability.inputs import collector_settings_from_boot

    config = command_runtime.config
    config.set_field("telemetry_otlp_enabled", True)
    for name in ("db_url", "redis_url", "redis_admin_password", "cluster_secret"):
        config.set_field(name, getattr(born.plane, name))
    command_runtime.machine.set(name="test-machine")
    rendered = oc.generate_config(
        _REPO,
        born.home,
        _ROLES,
        settings=collector_settings_from_boot(config),
        database_factory=operator_database,
        machine=command_runtime.machine,
    )
    secret = ledger.read_secret(born.home, delivery.active_generation(born.home))
    credentials = {
        "gateway generation": secret.roles.gateway.password,
        "runner generation": secret.roles.runner.password,
        "pooler admin": delivery.read_pooler_admin(born.home).password,
    }
    leaked = [name for name, value in credentials.items() if value in rendered]
    assert leaked == [], f"collector config carries credentials: {leaked}"
    return yaml.safe_load(rendered)["receivers"]["postgresql"]


def _dial_as_receiver(receiver: dict[str, Any], database: str) -> psycopg.Connection[Any]:
    """Dial exactly as otelcol-contrib's postgresql receiver builds its lib/pq
    DSN: `host:port` endpoint, `/` prefixed for the unix transport."""
    host, _, port = receiver["endpoint"].rpartition(":")
    if receiver["transport"] == "unix":
        host = "/" + host.lstrip("/")
    return psycopg.connect(
        host=host,
        port=port,
        user=receiver["username"],
        password=receiver["password"],
        dbname=database,
        sslmode="disable",
        connect_timeout=5,
        autocommit=True,
    )


def _scrape_like_the_receiver(receiver: dict[str, Any]) -> None:
    """The receiver's cluster-wide queries (on `postgres`) and per-database ones."""
    with _dial_as_receiver(receiver, "postgres") as conn:
        # Other sessions' activity/replication rows need the stats role.
        assert conn.execute("SELECT pg_has_role('pg_read_all_stats', 'USAGE')").fetchone() == (
            True,
        )
        sizes = conn.execute(
            "SELECT datname, pg_database_size(datname) FROM pg_catalog.pg_database"
            " WHERE datistemplate = false"
        ).fetchall()
        assert {name for name, _size in sizes} >= {"postgres", "meiva"}
        conn.execute("SELECT datname, count(*) FROM pg_stat_activity GROUP BY datname")
        conn.execute(
            "SELECT coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn), -1)"
            " FROM pg_stat_replication"
        )
        conn.execute("SELECT coalesce(last_archived_time, CURRENT_TIMESTAMP) FROM pg_stat_archiver")
        conn.execute("SHOW max_connections")
    for database in receiver["databases"]:
        with _dial_as_receiver(receiver, database) as conn:
            tables = conn.execute(
                "SELECT relname, pg_relation_size(relid) FROM pg_stat_user_tables"
            ).fetchall()
            assert "agents" in {name for name, _size in tables}
            conn.execute("SELECT * FROM pg_statio_user_tables")
            conn.execute("SELECT pg_relation_size(indexrelid) FROM pg_stat_user_indexes")


def test_collector_postgres_receiver_keeps_no_credential(
    born: Born, operator_database: OperatorDatabaseFactory, *, command_runtime: CommandRuntime
) -> None:
    """The collector's PostgreSQL receiver logs in as the stable monitoring
    role by `peer` over the owner-only socket: no password at rest in its
    config and no application data."""
    receiver = _collector_postgres_receiver(
        born, operator_database=operator_database, command_runtime=command_runtime
    )
    assert receiver["username"] == authority.MONITOR_ROLE
    assert receiver["transport"] == "unix"
    assert receiver["databases"] == ["meiva"]
    _scrape_like_the_receiver(receiver)
    with _dial_as_receiver(receiver, "meiva") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT id FROM agents LIMIT 1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO agents (id) VALUES (920001)")
    # No password exists for the role: TCP (SCRAM) never admits it.
    assert_refused(
        host="127.0.0.1",
        port=born.pg_port,
        user=authority.MONITOR_ROLE,
        password=receiver["password"],
        dbname="meiva",
    )

    with born.admin() as conn:
        authority.check_invariant(
            conn, born.home, database="meiva", readonly_grantees=completion.READONLY_GRANTEES
        )
    # The unchanged config keeps scraping.
    assert (
        _collector_postgres_receiver(
            born, operator_database=operator_database, command_runtime=command_runtime
        )
        == receiver
    )
    _scrape_like_the_receiver(receiver)


def test_direct_exemption_dials_postgres_as_the_delivered_login(
    born: Born, command_runtime: CommandRuntime
) -> None:
    """With pooling on, the admin-plane `direct=True` dial swaps only the port to
    the real Postgres (this home's own record) and keeps the delivered login."""

    # meiva:note
    # The factory builds each handle's dial slice from the live process settings, which the suite
    # runs with pooling off; the handle opener is where the factory takes its slice from outside.
    def open_pooled(
        config: DbConfig, gate: ProcessDbGate, direct_url: ClusterDirectUrl
    ) -> Database:
        return open_operator_database(replace(config, pgbouncer_enabled=True), gate, direct_url)

    pooled = operator_database_factory(
        local_host=command_runtime.machine.host, open_database=open_pooled
    )
    pooled.adopt_login(born.dsn("gateway"))
    handle = pooled()
    assert handle.direct_url() == url_with_userinfo(
        born.endpoint().replace(str(born.pooler_port), str(born.pg_port)), *born.login("gateway")
    )
    with handle.connect(direct=True) as conn:
        assert conn.execute("SELECT inet_server_port(), session_user").fetchone() == (
            born.pg_port,
            born.login("gateway")[0],
        )
    with handle.connect() as conn:  # pooled: the one access URL
        assert conn.execute("SELECT session_user").fetchone() == (born.login("gateway")[0],)
