# meiva:note
# Real bring-up of a per-cluster Postgres+Redis instance, end to end.
#
# Exercises `cluster_instance` and the gateway entry in `bringup` against the host's real
# `postgres` and `redis-server`, under the test's own home: initdb a fresh per-cluster Postgres,
# start it on an ephemeral port with the always-authenticated posture (peer for the OS user on the
# owner-only socket, SCRAM for every other role), start a per-cluster Redis with requirepass = an
# independent Redis-admin password, provision the NOLOGIN owner + db + schema (the `meiva_tinst`
# identifier here, passed as data) and check the runtime Redis identity. Write generations and the
# pooler are covered by cli/commands/tests/test_single_box.py.

from __future__ import annotations

import dataclasses
import getpass
import os
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg
import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from base import cluster
from base.clock import SYSTEM_TIMERS
from base.cluster import provision_database
from base.cluster.checkpoint_schema import PostgresCheckpointSchema
from base.cluster.dataplane.pg_admin import pg_admin_url
from base.cluster.dataplane.pg_tools import pg_shm_args, pg_start_env, pg_tool
from base.cluster.machine import MachineIdentityReader
from base.paths import meiva_home
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.bringup import ensure_gateway_data_plane
from cli.commands.data_plane.tests.native_cluster import (
    ADMIN_PASSWORD,
    BEARER,
    NATIVE_TEST_MACOS,
    RUNTIME_PASSWORD,
    NativeCluster,
    redis_command,
    redis_from_url,
    redis_ping,
    shutdown_redis,
)
from cli.commands.data_plane.tests.native_cluster import (
    native_cluster as native_cluster,
)
from cli.commands.data_plane.tests.native_cluster import (
    retained_children as retained_children,
)
from cli.database import OperatorDatabaseFactory
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime


def _storage(cluster_: NativeCluster, machine: MachineIdentityReader) -> int:
    return instance.ensure_cluster_storage(
        cluster_.plane,
        home=cluster_.home,
        pg_port=cluster_.pg_port,
        redis_port=cluster_.redis_port,
        redis_password=RUNTIME_PASSWORD,
        redis_user=cluster_.identity,
        retained_children=cluster_.retained_children,
        machine=machine,
        macos=NATIVE_TEST_MACOS,
        timers=SYSTEM_TIMERS,
        environ=os.environ,
    )


# meiva:note
# The OS-user administrator over the owner-only socket (peer).
def _admin(cluster_: NativeCluster, database: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host=str(instance.pg_socket_dir(cluster_.home)),
        port=cluster_.pg_port,
        dbname=database,
        autocommit=True,
    )


def test_per_cluster_instance_bringup(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _storage(native_cluster, command_machine) == 0
    admin_url = pg_admin_url(native_cluster.pg_port, home=meiva_home())

    # meiva:note
    # Provision the NOLOGIN owner + db + schema against the cluster's own instance, as the
    # administrator over its owner-only socket. The identifier is passed as data.
    provision_database(
        native_cluster.identity, base_admin_url=admin_url, expected_data_dir=native_cluster.pg_data
    )
    cluster.ensure_checkpoint_schema(
        native_cluster.identity,
        base_admin_url=admin_url,
        database_created=True,
        expected_data_dir=native_cluster.pg_data,
        schema=PostgresCheckpointSchema(),
    )

    with _admin(native_cluster, native_cluster.identity) as conn:
        # meiva:note
        # The owner can never log in and carries no password.
        assert conn.execute(
            "SELECT rolcanlogin, rolpassword IS NULL FROM pg_authid WHERE rolname = %s",
            (native_cluster.identity,),
        ).fetchone() == (False, True)
        # meiva:note
        # the schema applied (schema_migrations is the last table created)
        assert conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations'"
        ).fetchone()

    # meiva:note
    # No password-less or owner door over TCP (the lock that keeps other clusters out).
    for user, password in (
        (native_cluster.identity, ""),
        (native_cluster.identity, "wrong"),
        (getpass.getuser(), ""),
    ):
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(
                host="127.0.0.1",
                port=native_cluster.pg_port,
                user=user,
                password=password,
                dbname=native_cluster.identity,
            )

    # meiva:note
    # Runtime Redis identity: the meiva_tinst ACL user.
    runtime_url = native_cluster.plane.redis_url
    with redis_from_url(runtime_url) as runtime:
        assert redis_ping(runtime)
    wrong = f"redis://{native_cluster.identity}:{BEARER}@127.0.0.1:{native_cluster.redis_port}/0"
    with (
        redis_from_url(wrong) as wrong_bearer,
        pytest.raises(redis.AuthenticationError),
    ):
        redis_ping(wrong_bearer)


# meiva:note
# A second ensure on an already-running instance is a no-op success (the warm `meiva start` path).
def test_bringup_is_idempotent(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    for _ in range(2):
        assert _storage(native_cluster, command_machine) == 0


# meiva:note
# The gateway entry reads the ports from the home's record and each identity from its own URL
# (different Postgres and Redis names), publishes the telemetry token the station probe reads, and
# restores Redis's own ACL after a real RDB-only shutdown.
def test_the_entry_brings_up_the_instance_on_the_recorded_ports_with_the_url_identities(
    native_cluster: NativeCluster,
    command_machine: MachineIdentityReader,
    operator_database: OperatorDatabaseFactory,
) -> None:
    native_cluster.born()
    plane = dataclasses.replace(
        native_cluster.plane,
        db_url=f"postgresql://meiva_main@127.0.0.1:{native_cluster.pg_port}/meiva_main",
        redis_url=f"redis://meiva:{RUNTIME_PASSWORD}@127.0.0.1:{native_cluster.redis_port}/0",
    )

    def start() -> int:
        return ensure_gateway_data_plane(
            plane,
            home=native_cluster.home,
            retained_children=native_cluster.retained_children,
            database_factory=operator_database,
            machine=command_machine,
            macos=NATIVE_TEST_MACOS,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
        )

    assert start() == 0
    provision_database(
        "meiva_main",
        base_admin_url=pg_admin_url(native_cluster.pg_port, home=meiva_home()),
        expected_data_dir=native_cluster.pg_data,
    )
    with _admin(native_cluster, "meiva_main") as conn:
        assert conn.execute("SELECT current_database()").fetchone() == ("meiva_main",)
    with redis_from_url(plane.redis_url) as client:
        assert redis_ping(client)
        assert redis_command(client, "SET", "continuation", "pending") in (True, b"OK", "OK")
    shutdown_redis(native_cluster.redis_port, ADMIN_PASSWORD, save=True)
    assert (native_cluster.home / "redis" / "dump.rdb").is_file()

    assert start() == 0

    with redis_from_url(plane.redis_url) as client:
        assert redis_command(client, "GET", "continuation") == b"pending"
    with redis.Redis(port=native_cluster.redis_port, password=ADMIN_PASSWORD) as admin:
        assert redis_command(admin, "ACL", "GETUSER", "meiva") is not None
        assert redis_command(admin, "ACL", "GETUSER", "meiva_main") is None


# meiva:note
# The official bring-up of a born home serves a Redis that refuses an unauthenticated client; the
# runtime ACL user and the admin user authenticate.
def test_a_fresh_single_box_redis_refuses_unauthenticated_connections(
    native_cluster: NativeCluster,
    command_machine: MachineIdentityReader,
    operator_database: OperatorDatabaseFactory,
) -> None:
    native_cluster.born()
    assert (
        ensure_gateway_data_plane(
            native_cluster.plane,
            home=native_cluster.home,
            retained_children=native_cluster.retained_children,
            database_factory=operator_database,
            machine=command_machine,
            macos=NATIVE_TEST_MACOS,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
        )
        == 0
    )
    port = native_cluster.redis_port
    with (
        redis.Redis(port=port, retry=Retry(NoBackoff(), 0)) as anonymous,
        pytest.raises(redis.AuthenticationError),
    ):
        redis_ping(anonymous)
    with redis_from_url(native_cluster.plane.redis_url) as runtime:
        assert redis_ping(runtime)
    with redis.Redis(port=port, password=ADMIN_PASSWORD) as administrator:
        assert redis_ping(administrator)
    conf = (native_cluster.home / "redis" / "redis.conf").read_text()
    assert f'requirepass "{ADMIN_PASSWORD}"' in conf


# meiva:note
# The foreign instance lives in a short `/tmp` directory: a unix socket path is capped near a
# hundred bytes, which a pytest temp path overruns.
# meiva:note
# TCP ownership does not make another directory's equal Unix port ours.
def test_unix_only_foreign_postgres_same_port_cannot_receive_provisioning(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert (
        instance.start_pg(
            native_cluster.plane,
            native_cluster.pg_port,
            home=native_cluster.home,
            retained_children=native_cluster.retained_children,
            machine=command_machine,
            timers=SYSTEM_TIMERS,
        )
        == 0
    )
    pg_port = native_cluster.pg_port
    with tempfile.TemporaryDirectory(prefix="meiva-pg-foreign-", dir="/tmp") as temporary:
        directory = Path(temporary)
        data = directory / "data"
        instance.initdb(data, os_user=getpass.getuser(), home=native_cluster.home)
        subprocess.run(
            [
                str(pg_tool("pg_ctl", home=meiva_home())),
                "-D",
                str(data),
                "-l",
                str(directory / "pg.log"),
                "-w",
                "start",
                "-o",
                f"-p {pg_port} -c listen_addresses='' "
                f"-c unix_socket_directories={directory} {pg_shm_args()}",
            ],
            check=True,
            capture_output=True,
            env=pg_start_env(),
        )
        try:
            foreign_url = (
                f"postgresql://{getpass.getuser()}@/postgres?host={directory}&port={pg_port}"
            )
            canonical_socket = instance.pg_socket_dir(native_cluster.home) / f".s.PGSQL.{pg_port}"
            canonical_socket.unlink()
            isready = str(pg_tool("pg_isready", home=meiva_home()))
            assert instance.pg_running(isready, pg_port, "127.0.0.1"), "the TCP instance is ready"
            with pytest.raises(psycopg.OperationalError):
                cluster.ensure_cluster_role(
                    "must_not_create",
                    base_admin_url=pg_admin_url(pg_port, home=meiva_home()),
                    expected_data_dir=native_cluster.pg_data,
                )
            with pytest.raises(RuntimeError, match="foreign Unix socket"):
                cluster.ensure_cluster_role(
                    "must_not_create",
                    base_admin_url=foreign_url,
                    expected_data_dir=native_cluster.pg_data,
                )
            # meiva:note
            # Even the expected pathname cannot authorize a foreign backend.
            canonical_socket.symlink_to(directory / f".s.PGSQL.{pg_port}")
            with pytest.raises(RuntimeError, match="backend is not owned"):
                cluster.ensure_cluster_role(
                    "must_not_create",
                    base_admin_url=pg_admin_url(pg_port, home=meiva_home()),
                    expected_data_dir=native_cluster.pg_data,
                )
            with psycopg.connect(foreign_url) as conn:
                assert (
                    conn.execute(
                        "SELECT 1 FROM pg_roles WHERE rolname = 'must_not_create'"
                    ).fetchone()
                    is None
                )
        finally:
            subprocess.run(
                [
                    str(pg_tool("pg_ctl", home=meiva_home())),
                    "-D",
                    str(data),
                    "-m",
                    "immediate",
                    "stop",
                ],
                check=True,
                capture_output=True,
            )


# meiva:note
# (address, auth_method) of the host lines in the loaded hba file, in file order.
def _active_hba(cluster_: NativeCluster) -> list[tuple[str, str]]:
    with psycopg.connect(pg_admin_url(cluster_.pg_port, home=meiva_home())) as conn:
        rows = conn.execute(
            "SELECT address, auth_method FROM pg_hba_file_rules "
            "WHERE type = 'host' ORDER BY line_number"
        ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


# meiva:note
# First-start birth of a box that declares no reachable address: the hba has loopback SCRAM lines
# only (no reachable/cidr reach) and the running server refuses a password-less TCP dial even for
# the OS-user superuser.
def test_birth_hba_of_a_single_box_has_loopback_lines_only(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _storage(native_cluster, command_machine) == 0
    assert _active_hba(native_cluster) == [
        ("127.0.0.1", "scram-sha-256"),
        ("::1", "scram-sha-256"),
    ]
    with pytest.raises(psycopg.OperationalError, match="password"):
        psycopg.connect(
            host="127.0.0.1",
            port=native_cluster.pg_port,
            user=getpass.getuser(),
            password="",
            dbname="postgres",
        )


# meiva:note
# A legacy postmaster: trust everywhere, reloaded into the running server.
def _legacy_trust_loaded(cluster_: NativeCluster) -> None:
    (cluster_.pg_data / "pg_hba.conf").write_text(
        "local all all trust\nhost all all 127.0.0.1/32 trust\n"
    )
    with psycopg.connect(
        pg_admin_url(cluster_.pg_port, home=meiva_home()), autocommit=True
    ) as conn:
        conn.execute("SELECT pg_reload_conf()")


# meiva:note
# A running server keeps its last-loaded hba until reloaded. A postmaster that still serves a legacy
# trust hba must, after `start_pg` rewrites the file, enforce passwords before the start proceeds:
# the loaded proof, not the file on disk.
def test_a_rewritten_hba_is_reloaded_into_the_running_server(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _storage(native_cluster, command_machine) == 0
    _legacy_trust_loaded(native_cluster)
    deadline = time.monotonic() + 10
    while True:
        try:
            psycopg.connect(
                host="127.0.0.1",
                port=native_cluster.pg_port,
                user=getpass.getuser(),
                dbname="postgres",
            ).close()
            break
        except psycopg.OperationalError:
            assert time.monotonic() < deadline, "legacy trust hba never loaded"
            time.sleep(0.05)

    assert _storage(native_cluster, command_machine) == 0

    with pytest.raises(psycopg.OperationalError, match="password"):
        psycopg.connect(
            host="127.0.0.1",
            port=native_cluster.pg_port,
            user=getpass.getuser(),
            password="",
            dbname="postgres",
        )


# meiva:note
# The loaded-hba proof is behavioral: a postmaster that still admits a password-less dial (a trust
# line loaded) fails the proof, whatever file is on disk.
def test_the_hba_proof_refuses_a_postmaster_still_serving_trust(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _storage(native_cluster, command_machine) == 0
    socket_dir = instance.pg_socket_dir(native_cluster.home)
    instance.require_authenticated_hba(
        native_cluster.pg_port, "127.0.0.1", socket_dir=socket_dir, timers=SYSTEM_TIMERS
    )

    _legacy_trust_loaded(native_cluster)

    with pytest.raises(RuntimeError, match="does not enforce password authentication"):
        instance.require_authenticated_hba(
            native_cluster.pg_port, "127.0.0.1", socket_dir=socket_dir, timers=SYSTEM_TIMERS
        )


def test_redis_is_reachable_with_the_admin_password_only(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _storage(native_cluster, command_machine) == 0
    port = native_cluster.redis_port
    assert instance.redis_reachable(ADMIN_PASSWORD, port, "127.0.0.1")
    assert not instance.redis_reachable("not-the-admin-password", port, "127.0.0.1")
    with redis.Redis(port=port, password=ADMIN_PASSWORD, decode_responses=True) as admin:
        assert redis_command(admin, "ACL", "WHOAMI") == "default"
