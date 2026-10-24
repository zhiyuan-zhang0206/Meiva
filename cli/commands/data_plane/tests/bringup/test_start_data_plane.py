# meiva:note
# The start's data-plane steps that prepare storage: the gateway schema and the memory table.
#
# The order of the completion's steps, its refusals and the admission of a start are plan facts
# (`instance/test_storage_plan.py`); the birth and the ordinary start of a whole home against a
# real Postgres, pooler and Redis are `cli/commands/tests/test_single_box.py`. What is run here is
# each preparation step against a real database: a born home gets its owner, database, extension
# and checkpoint schema from the cluster's own instance, and the memory table follows the
# embedding provider's width.

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from cli.commands.data_plane import bringup, cluster_instance, completion
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import (
    remote_data_plane_config as remote_data_plane_config,
)
from cli.commands.data_plane.tests.native_cluster import NativeCluster
from cli.commands.data_plane.tests.native_cluster import native_cluster as native_cluster
from cli.commands.data_plane.tests.native_cluster import retained_children as retained_children
from cli.database import OperatorDatabaseFactory
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.derived.memory_indexer.embeddings.factory import get_descriptor

# meiva:note
# The vector width of a column: pgvector stores it as the column's type modifier.
_VECTOR_WIDTH = (
    "SELECT atttypmod FROM pg_attribute "
    "WHERE attrelid = 'memory_embeddings'::regclass AND attname = 'vector'"
)


@pytest.fixture
def memory_table(db_url: str) -> Iterator[str]:
    """The suite's database with no memory table before the case and none left behind."""
    drop = "DROP TABLE IF EXISTS memory_embeddings"
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(drop)
    yield db_url
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(drop)


def _start_instance(
    cluster: NativeCluster, machine: MachineIdentity, factory: OperatorDatabaseFactory
) -> None:
    cluster.born()
    rc = bringup.ensure_gateway_data_plane(
        cluster.plane,
        home=cluster.home,
        retained_children=cluster.retained_children,
        database_factory=factory,
        machine=machine,
    )
    assert rc == 0


def _admin(cluster: NativeCluster, database: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(
        host=str(cluster_instance.pg_socket_dir(cluster.home)),
        port=cluster.pg_port,
        dbname=database,
        autocommit=True,
    )


def test_a_born_home_gets_the_extension_its_postgres_carries_and_the_checkpoint_schema(
    native_cluster: NativeCluster,
    command_machine: MachineIdentity,
    operator_database: OperatorDatabaseFactory,
) -> None:
    _start_instance(native_cluster, command_machine, operator_database)

    bringup.prepare_gateway_schema(native_cluster.plane, home=native_cluster.home)

    with _admin(native_cluster, native_cluster.identity) as conn:
        assert conn.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (native_cluster.identity,)
        ).fetchone() == (False,)
        available = conn.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
        ).fetchone()
        installed = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
        assert (installed is not None) == (available is not None)
        assert conn.execute("SELECT to_regclass('checkpoints') IS NOT NULL").fetchone() == (True,)


def test_a_second_preparation_of_the_same_home_is_a_no_op(
    native_cluster: NativeCluster,
    command_machine: MachineIdentity,
    operator_database: OperatorDatabaseFactory,
) -> None:
    _start_instance(native_cluster, command_machine, operator_database)
    bringup.prepare_gateway_schema(native_cluster.plane, home=native_cluster.home)
    bringup.prepare_gateway_schema(native_cluster.plane, home=native_cluster.home)


def test_a_home_past_its_first_start_prepares_no_schema(
    native_cluster: NativeCluster,
    command_machine: MachineIdentity,
    operator_database: OperatorDatabaseFactory,
) -> None:
    _start_instance(native_cluster, command_machine, operator_database)
    native_cluster.write_intent("provisioned")

    bringup.prepare_gateway_schema(native_cluster.plane, home=native_cluster.home)

    with _admin(native_cluster, "postgres") as conn:
        databases = conn.execute("SELECT datname FROM pg_database").fetchall()
    assert (native_cluster.identity,) not in databases


def test_a_remote_plane_prepares_no_gateway_schema(
    remote_data_plane_config: DataPlaneConfig, tmp_path: Path
) -> None:
    bringup.prepare_gateway_schema(remote_data_plane_config, home=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_home_with_no_start_intent_prepares_no_schema(native_cluster: NativeCluster) -> None:
    bringup.prepare_gateway_schema(native_cluster.plane, home=native_cluster.home)
    assert not native_cluster.pg_data.exists()


# meiva:note
# A home past its first start that has no database authority ledger was born before the data plane
# always authenticated: nothing converts it, and nothing is dialled first.
def test_a_release_readiness_refuses_a_home_without_a_ledger_before_any_dial(
    native_cluster: NativeCluster,
    command_machine: MachineIdentity,
    operator_database: OperatorDatabaseFactory,
) -> None:
    native_cluster.born()
    native_cluster.write_intent("provisioned")

    with pytest.raises(RuntimeError, match="no database authority ledger"):
        completion.complete_gateway_data_plane(
            native_cluster.plane,
            home=native_cluster.home,
            environment={},
            refresh_schema=False,
            database_factory=operator_database,
            machine=command_machine,
        )


# meiva:note
# ─── the memory table (a derived cache keyed to the embedding provider's width) ──────────────


def test_a_backend_other_than_pgvector_prepares_no_table(
    memory_table: str,
    operator_database: OperatorDatabaseFactory,
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    plane = dataclasses.replace(remote_data_plane_config, memory_search_backend="numpy")

    bringup.prepare_memory_vectors(plane, database_factory=operator_database)

    with psycopg.connect(memory_table) as conn:
        assert conn.execute("SELECT to_regclass('memory_embeddings')").fetchone() == (None,)


# meiva:note
# A remote-managed plane has no local owner authority; its provider URL carries the table DDL,
# exactly as it carries the plane's migrations.
def test_a_remote_plane_prepares_the_table_through_its_provider_url(
    memory_table: str,
    operator_database: OperatorDatabaseFactory,
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    plane = dataclasses.replace(remote_data_plane_config, memory_search_backend="pgvector")
    with psycopg.connect(memory_table, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

    bringup.prepare_memory_vectors(plane, database_factory=operator_database)

    with psycopg.connect(memory_table) as conn:
        width = conn.execute(_VECTOR_WIDTH).fetchone()
    assert width == (get_descriptor(plane.embedding_backend).dim,)


def test_a_table_of_another_width_is_rebuilt_at_the_providers_width(
    memory_table: str,
    operator_database: OperatorDatabaseFactory,
    remote_data_plane_config: DataPlaneConfig,
) -> None:
    plane = dataclasses.replace(remote_data_plane_config, memory_search_backend="pgvector")
    with psycopg.connect(memory_table, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("CREATE TABLE memory_embeddings (pk text PRIMARY KEY, vector vector(3))")

    bringup.prepare_memory_vectors(plane, database_factory=operator_database)

    with psycopg.connect(memory_table) as conn:
        width = conn.execute(_VECTOR_WIDTH).fetchone()
    assert width == (get_descriptor(plane.embedding_backend).dim,)
