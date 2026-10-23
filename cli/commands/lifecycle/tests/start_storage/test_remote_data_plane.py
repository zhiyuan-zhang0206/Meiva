# meiva:note
# Remote-managed data plane (Task #1752): start / stop / status degrade to reachability probes and
# clear skips instead of managing a foreign service.
#
# The local instance management surface (`ensure_cluster_storage`, `stop_cluster_instance`, the
# `meiva status` data-plane section) must never run against a data plane whose URLs name another host:
# the URL is the switch, and the management plane keys off `DataPlaneConfig.is_remote`. Were the local
# bring-up reached, the real `ensure_cluster_storage` would refuse this home for want of Redis
# credentials, so a start that returns zero never got there.
#
# The probes dial real endpoints: the suite's throwaway Postgres and Redis are the ones that answer,
# a port nothing listens on is the one that refuses.

from __future__ import annotations

import dataclasses
import socket
from pathlib import Path

import pytest

from cli.commands.data_plane import bringup
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.commands.data_plane.tests.data_plane_records import remote_cluster_record
from cli.commands.data_plane.tests.data_plane_records import (
    remote_data_plane_config as remote_data_plane_config,
)
from cli.commands.lifecycle.tests.start_storage.owned_postgres import write_record
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime


# meiva:note
# A database URL on a port nothing listens on.
def _refused_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"postgresql://meiva:pw@127.0.0.1:{port}/meiva"


@pytest.fixture
def registered_home(unit_home: Path) -> Path:
    write_record(unit_home, remote_cluster_record().ports)
    return unit_home


# meiva:note
# ─── meiva start: skip local instance bring-up, probe the URLs ─────────────────


def test_start_remote_skips_local_instance_and_probes(
    db_url: str,
    redis_url: str,
    registered_home: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    config = dataclasses.replace(remote_data_plane_config, redis_url=redis_url)
    assert config.is_remote, "the database URL still names another host"

    rc = bringup.ensure_gateway_data_plane(
        config, database_factory=operator_database, machine=command_runtime.machine
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "remote-managed" in out
    assert "skipping local instance bring-up" in out
    assert "remote data plane reachable" in out
    assert not (registered_home / "pg").exists() and not (registered_home / "redis").exists()


def test_start_remote_unreachable_fails_fast_with_dial_detail(
    db_url: str,
    registered_home: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    operator_database.adopt_login(_refused_url())

    rc = bringup.ensure_gateway_data_plane(
        remote_data_plane_config,
        database_factory=operator_database,
        machine=command_runtime.machine,
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "remote data plane unreachable" in err
    assert "10.9.8.7" in err
    assert "MEIVA_DB_URL" in err
    assert not (registered_home / "pg").exists()


def test_start_remote_redis_unreachable_fails_after_the_database_answers(
    db_url: str,
    redis_url: str,
    registered_home: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    refused_redis = _refused_url().replace("postgresql://", "redis://").rsplit("/", 1)[0] + "/0"
    config = dataclasses.replace(remote_data_plane_config, redis_url=refused_redis)

    rc = bringup.ensure_gateway_data_plane(
        config, database_factory=operator_database, machine=command_runtime.machine
    )

    assert rc == 1
    assert "remote data plane unreachable: redis" in capsys.readouterr().err
