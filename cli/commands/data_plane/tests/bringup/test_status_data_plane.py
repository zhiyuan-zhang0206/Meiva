# meiva:note
# `meiva status` and the remote-managed probes against real services.
#
# The local plane is a real Postgres and Redis in the test's home (`native_cluster`); the login
# the pooled front door dials is a role created on that very instance. A remote-managed plane is
# a config whose URLs name another host while the services that answer are the suite's own
# throwaway database and Redis, so the dial path (and its failure details) is the real one. The
# lines those readings become are asserted in `test_plane_lines.py`.

from __future__ import annotations

import dataclasses
import socket
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from base.cluster.machine import MachineIdentity
from cli.commands.data_plane import bringup, cluster_instance
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import (
    remote_data_plane_config as remote_data_plane_config,
)
from cli.commands.data_plane.tests.native_cluster import (
    NATIVE_TEST_MACOS,
    RUNTIME_PASSWORD,
    NativeCluster,
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

_LOGIN = "status_probe"
_LOGIN_PASSWORD = "status-probe-password"  # noqa: S105


def _start_storage(cluster: NativeCluster, machine: MachineIdentity) -> None:
    rc = cluster_instance.ensure_cluster_storage(
        cluster.plane,
        home=cluster.home,
        pg_port=cluster.pg_port,
        redis_port=cluster.redis_port,
        redis_password=RUNTIME_PASSWORD,
        redis_user=cluster.identity,
        retained_children=cluster.retained_children,
        machine=machine,
        macos=NATIVE_TEST_MACOS,
    )
    assert rc == 0


# meiva:note
# A password login on the cluster's own instance, the delivered gateway login's stand-in.
def _create_login(cluster: NativeCluster) -> None:
    with psycopg.connect(
        host=str(cluster_instance.pg_socket_dir(cluster.home)),
        port=cluster.pg_port,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(_LOGIN), sql.Literal(_LOGIN_PASSWORD)
            )
        )


def _dial(cluster: NativeCluster, password: str) -> str:
    return f"postgresql://{_LOGIN}:{password}@127.0.0.1:{cluster.pg_port}/postgres"


def test_a_running_plane_whose_login_answers_is_green(
    native_cluster: NativeCluster,
    command_machine: MachineIdentity,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _start_storage(native_cluster, command_machine)
    _create_login(native_cluster)
    operator_database.adopt_login(_dial(native_cluster, _LOGIN_PASSWORD))

    bringup.print_data_plane_status(
        native_cluster.plane, home=native_cluster.home, database_factory=operator_database
    )

    out = capsys.readouterr().out
    assert f"✓ postgres (127.0.0.1:{native_cluster.pg_port})" in out
    assert f"✓ redis (127.0.0.1:{native_cluster.redis_port})" in out
    assert "pgbouncer" not in out


# meiva:note
# `pg_isready` alone answers without authenticating; the pooled `SELECT 1` as the delivered login is
# what tells.
def test_a_server_that_refuses_the_delivered_login_is_never_a_green_tick(
    native_cluster: NativeCluster,
    command_machine: MachineIdentity,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _start_storage(native_cluster, command_machine)
    _create_login(native_cluster)
    operator_database.adopt_login(_dial(native_cluster, "not-the-password"))

    bringup.print_data_plane_status(
        native_cluster.plane, home=native_cluster.home, database_factory=operator_database
    )

    out = capsys.readouterr().out
    assert f"✗ postgres (127.0.0.1:{native_cluster.pg_port}) reachable but connect failed" in out
    assert "password authentication failed" in out


def test_a_plane_that_is_not_running_is_unreachable_on_both_lines(
    native_cluster: NativeCluster,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bringup.print_data_plane_status(
        native_cluster.plane, home=native_cluster.home, database_factory=operator_database
    )

    out = capsys.readouterr().out
    assert f"✗ postgres (127.0.0.1:{native_cluster.pg_port}) unreachable" in out
    assert f"✗ redis (127.0.0.1:{native_cluster.redis_port})" in out


# meiva:note
# The pooler port is a registry fact only: with no record, status says it cannot resolve the port
# instead of printing a false `:0`.
def test_the_pooler_line_says_so_when_the_home_has_no_registry_record(
    native_cluster: NativeCluster,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plane = dataclasses.replace(native_cluster.plane, pgbouncer_enabled=True)

    bringup.print_data_plane_status(
        plane, home=native_cluster.home, database_factory=operator_database
    )

    out = capsys.readouterr().out
    assert "no registry record" in out and "127.0.0.1:0" not in out


# meiva:note
# The pooler's listen port comes from the home's record, never a settings cache; a home without a
# pooler admin entry reports the authority store's refusal on that very port.
def test_the_pooler_line_names_the_registry_port_and_why_its_console_is_unreadable(
    native_cluster: NativeCluster,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    native_cluster.born()
    plane = dataclasses.replace(native_cluster.plane, pgbouncer_enabled=True)
    listen = native_cluster.pooler_port

    bringup.print_data_plane_status(
        plane, home=native_cluster.home, database_factory=operator_database
    )

    out = capsys.readouterr().out
    assert f"pgbouncer (127.0.0.1:{listen}" in out
    assert "127.0.0.1:0" not in out


# meiva:note
# ─── a remote-managed plane: probe the URLs, no local pooler line ────────────────────────────


# meiva:note
# A foreign-host URL makes the data plane remote-managed (Task #1752): the status probes the URLs
# through their own dials and never the local instance.
def test_a_remote_plane_is_probed_through_its_urls_and_has_no_pooler_line(
    db_url: str,
    redis_url: str,
    operator_database: OperatorDatabaseFactory,
    remote_data_plane_config: DataPlaneConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plane = dataclasses.replace(
        remote_data_plane_config, redis_url=redis_url, pgbouncer_enabled=True
    )
    assert plane.is_remote

    bringup.print_data_plane_status(plane, home=tmp_path, database_factory=operator_database)

    out = capsys.readouterr().out
    assert "remote-managed" in out
    assert "✓ postgres (10.9.8.7:5432)" in out
    assert "✓ redis (127.0.0.1:" in out
    assert "pgbouncer" not in out


def _refused_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_remote_redis_that_refuses_is_reported_with_its_dial_detail(
    db_url: str,
    redis_access: None,
    operator_database: OperatorDatabaseFactory,
    remote_data_plane_config: DataPlaneConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plane = dataclasses.replace(
        remote_data_plane_config, redis_url=f"redis://meiva:pw@127.0.0.1:{_refused_port()}/0"
    )

    bringup.print_data_plane_status(plane, home=tmp_path, database_factory=operator_database)

    out = capsys.readouterr().out
    assert "✓ postgres (10.9.8.7:5432)" in out
    assert "✗ redis (127.0.0.1:" in out and "connect failed" in out
