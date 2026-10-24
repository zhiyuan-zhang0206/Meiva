# meiva:note
# The gateway's data-plane step of `meiva start` brings up this cluster's own instance.
#
# Every cluster owns its Postgres+Redis; `ensure_gateway_data_plane` reads the cluster's record from
# the home's start intent and starts the per-cluster instance on the record's pg/redis ports. A
# missing record (defensive) is a hard error, not a silent fall-through onto some shared instance.
# The home here is a real born home (`prepare_identity` writes its intent and record); the native
# instance itself is brought up for real by `cli/commands/tests/test_single_box.py`.

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base import cluster
from base.host.db_authority.api import read_telemetry_token, telemetry_token, telemetry_token_path
from cli import start_identity
from cli.commands.data_plane import bringup
from cli.commands.data_plane import cluster_instance as _ci
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime


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
        )
    )
    start_identity.mark_phase(home, "provisioned")
    record = cluster.get_record(home)
    assert record is not None
    return record


# meiva:note
# A born cluster: the per-cluster instance on the record's exact pg/redis ports, with each
# data-plane identity read from its own URL.
def test_gateway_data_plane_brings_up_own_instance(
    monkeypatch: pytest.MonkeyPatch,
    unit_home: Path,
    seed_write_generation: Callable[[Path], Any],
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
    data_plane_config: DataPlaneConfig,
) -> None:
    record = _born(unit_home)
    # meiva:note
    # an established home: its database authority ledger exists
    seed_write_generation(unit_home)
    (unit_home / ".env").write_text("MEIVA_REDIS_PASSWORD=redis-runtime\n")
    # meiva:note
    # The credentials are literals of a throwaway config that never leaves the test process, so
    # the hard-coded-password rule has nothing to protect here.
    config = dataclasses.replace(
        data_plane_config,
        cluster_secret="bearer",  # noqa: S106
        redis_admin_password="redis-admin",  # noqa: S106
        db_url="postgresql://meiva_main:sek@127.0.0.1:23011/meiva_main",
        redis_url="redis://meiva:sek@127.0.0.1:23012/0",
    )
    own_calls: list[dict[str, object]] = []
    stored_with: list[DataPlaneConfig] = []

    def _record_storage(config: DataPlaneConfig, **kwargs: object) -> int:
        stored_with.append(config)
        own_calls.append(kwargs)
        return 0

    monkeypatch.setattr(_ci, "ensure_cluster_storage", _record_storage)

    assert (
        bringup.ensure_gateway_data_plane(
            config,
            home=unit_home,
            retained_children=[],
            database_factory=operator_database,
            machine=command_runtime.machine,
        )
        == 0
    )
    # meiva:note
    # The root that holds the secret published the telemetry token the station probe reads.
    assert read_telemetry_token(unit_home.resolve()) == telemetry_token("bearer")
    # meiva:note
    # The ports come from the home's record, the identity from the URLs, never a name derivation.
    assert own_calls == [
        {
            "home": unit_home.resolve(),
            "pg_port": record.ports["postgres"],
            "redis_port": record.ports["redis"],
            "redis_password": "redis-runtime",
            "redis_user": "meiva",
            "retained_children": [],
            "machine": command_runtime.machine,
        }
    ]
    # meiva:note
    # the admin login travels inside the config
    assert stored_with == [config]


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
        )

    assert not telemetry_token_path(unit_home.resolve()).exists()
