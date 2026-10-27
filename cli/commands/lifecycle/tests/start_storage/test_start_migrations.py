# meiva:note
# The migration step of `meiva start` against real Postgres.
#
# Which authority applies the migrations is a plan (`../start_plans/test_cold_start_plan.py`). Here
# the step runs: on a home-owned plane it dials the owner socket and converges the schema; on a
# remote-managed plane it dials the ordinary access URL and never asks for a local owner authority.
# Converging then repeating over an owned plane is shown, with the privilege checks around it, in
# `test_pg_owner_authority.py`.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from base.cluster import ensure_checkpoint_schema, provision_database
from base.cluster.checkpoint_schema import PostgresCheckpointSchema
from base.cluster.dataplane.pg_admin import pg_admin_url
from base.cluster.provision import CheckpointDependencyDriftError
from base.paths import meiva_home
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.commands.data_plane.tests.data_plane_records import (
    remote_data_plane_config as remote_data_plane_config,
)
from cli.commands.lifecycle.start.migrations import cmd_migrations_apply
from cli.commands.lifecycle.tests.start_storage.owned_postgres import Plane
from cli.commands.lifecycle.tests.start_storage.owned_postgres import owned_pg as owned_pg
from cli.commands.lifecycle.tests.start_storage.owned_postgres import plane as plane
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime


def _applied(plane: Plane, identity: str) -> list[str]:
    with psycopg.connect(plane.admin_on(identity)) as conn:
        return [row[0] for row in conn.execute("SELECT name FROM schema_migrations ORDER BY name")]


def test_an_owned_plane_converges_over_its_owner_socket(plane: Plane) -> None:
    identity = "meiva_mig"
    admin = pg_admin_url(plane.port, home=meiva_home())
    created = provision_database(identity, base_admin_url=admin, expected_data_dir=plane.data_dir)
    ensure_checkpoint_schema(
        identity,
        base_admin_url=admin,
        database_created=created,
        expected_data_dir=plane.data_dir,
        schema=PostgresCheckpointSchema(),
    )

    applied = plane.migrate(identity)

    assert applied and set(applied) <= set(_applied(plane, identity))
    assert plane.migrate(identity) == [], "a second step finds nothing pending"


@dataclass(frozen=True)
class _DriftedCheckpointSchema:
    migrations: Sequence[str]

    def setup(self, *, base_admin_url: str, identity: str, expected_data_dir: Path | None) -> None:
        raise AssertionError(f"setup is not called for {identity} on {base_admin_url}")


# meiva:note
# A new upstream checkpoint migration must first be mirrored in a Meiva migration. The gate
# runs before the database is dialed at all: the plane's database does not even exist yet.
def test_dependency_drift_fails_before_any_database_change(plane: Plane) -> None:
    drifted = _DriftedCheckpointSchema([*PostgresCheckpointSchema().migrations, "SELECT 1"])

    with pytest.raises(CheckpointDependencyDriftError, match="Meiva timestamp migration"):
        plane.migrate("meiva_drift", checkpoint_schema=drifted)

    with psycopg.connect(plane.admin_on("postgres")) as conn:
        databases = {row[0] for row in conn.execute("SELECT datname FROM pg_database")}
    assert "meiva_drift" not in databases


# meiva:note
# The plane's URL is the provider's only address for us: there is no local authority to ask
# for. The suite's database stands in for the provider's; nothing is pending, so the step
# applies nothing and verifies the checkpoint schema read-only.
def test_a_remote_managed_plane_is_dialed_through_the_access_url_never_an_owner_socket(
    db_url: str,
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
    remote_data_plane_config: DataPlaneConfig,
    unit_home: Path,
) -> None:
    assert remote_data_plane_config.is_remote
    del db_url

    applied = cmd_migrations_apply(
        database_factory=operator_database,
        machine=command_runtime.machine,
        config=remote_data_plane_config,
        home=unit_home,
    )

    assert applied == []
