# meiva:note
# The probes of a remote-managed data plane (Task #1752), against real endpoints.
#
# A foreign-host URL is the switch: startup, status and stop degrade to reachability probes of the
# URLs themselves and never manage a foreign service. The probes dial real endpoints: the suite's
# throwaway Postgres and Redis answer, a port nothing listens on refuses, a Redis that demands a
# password the URL does not carry rejects. What the probes' readings become is `test_plane_lines.py`.

from __future__ import annotations

import dataclasses
import socket

from base.deploy.tests.containers import redis_server
from cli.commands.data_plane import bringup
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.native_cluster import redis_command, redis_from_url
from cli.database import OperatorDatabaseFactory
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime


# meiva:note
# A loopback port nothing listens on.
def _refused_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_reachable_remote_postgres_reports_its_endpoint(
    db_url: str,
    operator_database: OperatorDatabaseFactory,
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    ok, line = bringup.remote_pg_reachable(
        remote_data_plane_config, database_factory=operator_database
    )
    assert (ok, line) == (True, "postgres (10.9.8.7:5432)")


def test_an_unreachable_remote_postgres_reports_the_dial_detail_and_never_raises(
    postgres_access: None,
    operator_database: OperatorDatabaseFactory,
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    operator_database.adopt_login(f"postgresql://meiva:pw@127.0.0.1:{_refused_port()}/meiva")

    ok, line = bringup.remote_pg_reachable(
        remote_data_plane_config, database_factory=operator_database
    )

    assert ok is False
    assert line.startswith("postgres (10.9.8.7:5432) connect failed: ")
    assert len(line.splitlines()) == 1


def test_a_reachable_remote_redis_reports_its_endpoint(
    redis_url: str, remote_data_plane_config: DataPlaneConfig
) -> None:
    config = dataclasses.replace(remote_data_plane_config, redis_url=redis_url)
    port = redis_url.rsplit(":", 1)[1].split("/", maxsplit=1)[0]
    assert bringup.remote_redis_reachable(config) == (True, f"redis (127.0.0.1:{port})")


def test_a_refused_remote_redis_reports_the_dial_detail(
    redis_access: None, remote_data_plane_config: DataPlaneConfig
) -> None:
    port = _refused_port()
    config = dataclasses.replace(
        remote_data_plane_config, redis_url=f"redis://meiva:pw@127.0.0.1:{port}/0"
    )

    ok, line = bringup.remote_redis_reachable(config)

    assert ok is False
    assert line.startswith(f"redis (127.0.0.1:{port}) connect failed: ")


# meiva:note
# On a remote/SaaS plane the URL userinfo is the only credential that exists.
def test_a_remote_redis_that_demands_a_password_the_url_lacks_is_unreachable(
    redis_access: None, remote_data_plane_config: DataPlaneConfig
) -> None:
    with redis_server() as url:
        with redis_from_url(url) as admin:
            redis_command(admin, "CONFIG", "SET", "requirepass", "needed")
        config = dataclasses.replace(remote_data_plane_config, redis_url=url)

        ok, line = bringup.remote_redis_reachable(config)

    assert ok is False and "connect failed" in line


def test_a_listener_check_sees_only_what_accepts_connections() -> None:
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        assert bringup.local_listener(port) is True
    assert bringup.local_listener(port) is False
