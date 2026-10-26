# meiva:note
# The gateway's data-plane step of `meiva start` brings up this cluster's own instance.
#
# Every cluster owns its Postgres+Redis; `ensure_gateway_data_plane` reads the cluster's record from
# the home's start intent and starts the per-cluster instance on the record's pg/redis ports. A
# missing record (defensive) is a hard error, not a silent fall-through onto some shared instance.
# The refusal cases use a real born home (`prepare_identity` writes its intent and record); the
# first case brings the native instance up for real.

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base import cluster
from base.clock import SYSTEM_TIMERS
from base.host.db_authority.api import read_telemetry_token, telemetry_token, telemetry_token_path
from cli import start_identity
from cli.commands.data_plane import bringup
from cli.commands.data_plane.cluster_instance import pg_socket_dir
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.commands.data_plane.tests.native_cluster import (
    BEARER,
    NATIVE_TEST_MACOS,
    NativeCluster,
    redis_from_url,
    redis_ping,
)
from cli.commands.data_plane.tests.native_cluster import native_cluster as native_cluster
from cli.commands.data_plane.tests.native_cluster import retained_children as retained_children
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.startup.tests.birth_facts import birth_facts


# meiva:note
# A home `meiva init` has configured and a first start provisioned: intent, record, phase.
def _born(home: Path) -> cluster.ClusterRecord:
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            home,
            home,
            frozenset({"gateway"}),
            {"MEIVA_MACHINE_NAME": "gw", "MEIVA_GATEWAY_URL": "http://127.0.0.1:1"},
            occupied_ports=frozenset(),
            birth=birth_facts(),
        )
    )
    start_identity.mark_phase(home, "provisioned")
    record = cluster.get_record(home)
    assert record is not None
    return record


# meiva:note
# A born cluster: the per-cluster instance comes up for real on the record's exact pg/redis ports,
# each data-plane identity read from its own URL, and the root that holds the secret publishes the
# telemetry token the station probe reads.
def test_gateway_data_plane_brings_up_own_instance(
    seed_write_generation: Callable[[Path], Any],
    operator_database: Callable[[], Any],
    native_cluster: NativeCluster,
    *,
    command_runtime: CommandRuntime,
) -> None:
    native_cluster.born()
    seed_write_generation(native_cluster.home)

    assert (
        bringup.ensure_gateway_data_plane(
            native_cluster.plane,
            home=native_cluster.home,
            retained_children=native_cluster.retained_children,
            database_factory=operator_database,
            machine=command_runtime.machine,
            macos=NATIVE_TEST_MACOS,
            timers=SYSTEM_TIMERS,
        )
        == 0
    )

    assert read_telemetry_token(native_cluster.home.resolve()) == telemetry_token(BEARER)
    with redis_from_url(native_cluster.plane.redis_url) as client:
        assert redis_ping(client), "redis answers on the record's port as the URL's identity"
    with psycopg.connect(
        host=str(pg_socket_dir(native_cluster.home)),
        port=native_cluster.pg_port,
        dbname="postgres",
        autocommit=True,
    ) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


# meiva:note
# A home born before the always-authenticated data plane (no ledger, not a first start in
# progress) is refused before any native effect, never converted by an ordinary start. The real
# storage step would stop a bring-up for want of Redis credentials, which this home lacks too.
def test_gateway_data_plane_refuses_a_home_without_a_ledger_before_any_effect(
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
    data_plane_config: DataPlaneConfig,
) -> None:
    _born(unit_home)

    assert (
        bringup.ensure_gateway_data_plane(
            data_plane_config,
            home=unit_home,
            retained_children=[],
            database_factory=operator_database,
            machine=command_runtime.machine,
            macos=command_runtime.host.platform.system == "darwin",
            timers=SYSTEM_TIMERS,
        )
        == 1
    )
    assert "no conversion exists" in capsys.readouterr().err
    assert not (unit_home / "pg").exists() and not (unit_home / "redis").exists()


# meiva:note
# A not-yet-registered home (defensive) is a hard error, never a bring-up.
def test_gateway_data_plane_no_record_is_error(
    unit_home: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
    data_plane_config: DataPlaneConfig,
) -> None:
    assert not (unit_home / "start-intent.json").exists()

    assert (
        bringup.ensure_gateway_data_plane(
            data_plane_config,
            home=unit_home,
            retained_children=[],
            database_factory=operator_database,
            machine=command_runtime.machine,
            macos=command_runtime.host.platform.system == "darwin",
            timers=SYSTEM_TIMERS,
        )
        == 1
    )
    assert not (unit_home / "pg").exists() and not (unit_home / "redis").exists()


def test_local_launch_without_owner_cannot_publish_authority(
    unit_home: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
    data_plane_config: DataPlaneConfig,
) -> None:
    _born(unit_home)

    with pytest.raises(ValueError, match="caller-owned child retention"):
        bringup.ensure_gateway_data_plane(
            data_plane_config,
            home=unit_home,
            retained_children=None,
            database_factory=operator_database,
            machine=command_runtime.machine,
            macos=command_runtime.host.platform.system == "darwin",
            timers=SYSTEM_TIMERS,
        )

    assert not telemetry_token_path(unit_home.resolve()).exists()
