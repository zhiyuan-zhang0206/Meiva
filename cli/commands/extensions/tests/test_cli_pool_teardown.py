# meiva:note
# The converge-side readers of the cluster extension registry, against a real Postgres.
#
# `materialize_cluster_extensions` and `adopt_local_extensions` open a connection pool on the
# operator's database, do their work and report instead of raising. What these tests hold fixed
# is that the pool is closed on every way out: a pool left for `ConnectionPool.__del__` prints
# "cannot join current thread" noise at interpreter exit and keeps its worker threads alive, so
# the evidence is that no `pool-N` thread survives the call. Each failure is a real one: a
# destination that is a file, a machine without a name.

from __future__ import annotations

import threading
from pathlib import Path

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.packages.extensions import install_registry
from base.packages.extensions import registry as cluster_registry
from base.packages.extensions.install_registry import tree_hash
from cli.commands.extensions.materialize import (
    adopt_local_extensions,
    materialize_cluster_extensions,
)
from cli.database import OperatorDatabaseFactory
from cli.database.tests.cli_tests import installed_machine_identity as installed_machine_identity
from cli.database.tests.cli_tests import operator_database as operator_database


def _pool_threads() -> set[str]:
    return {thread.name for thread in threading.enumerate() if thread.name.startswith("pool-")}


def _skill_tree(root: Path, body: str) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(f"---\nname: {root.name}\ndescription: {body}\n---\n\n{body}\n")
    return root


def _publish(db_conn: psycopg.Connection, root: Path) -> None:
    digest = tree_hash(root)
    cluster_registry.put_blob(
        db_conn, cluster_registry.pack_tree(root), name=root.name, content_hash=digest
    )
    cluster_registry.upsert(
        db_conn,
        name=root.name,
        kind="skill",
        source="https://example.invalid/x",
        content_hash=digest,
    )
    db_conn.commit()


def test_the_cluster_registry_lands_on_the_machine_and_the_pool_is_closed(
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(db_conn, _skill_tree(tmp_path / "published" / "alpha", "alpha body"))
    skills = tmp_path / "skills"
    before = _pool_threads()

    materialize_cluster_extensions(database_factory=operator_database, skills_root=skills)

    assert (skills / "alpha" / "SKILL.md").read_text().endswith("alpha body\n")
    assert capsys.readouterr().out.splitlines() == ["    landed: alpha"]
    assert _pool_threads() == before


def test_a_destination_that_cannot_hold_skills_is_reported_and_the_pool_is_closed(
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _publish(db_conn, _skill_tree(tmp_path / "published" / "alpha", "alpha body"))
    not_a_directory = tmp_path / "skills"
    not_a_directory.write_text("a file where the skills directory belongs\n")
    before = _pool_threads()

    materialize_cluster_extensions(database_factory=operator_database, skills_root=not_a_directory)

    assert "cluster registry unreachable" in capsys.readouterr().err
    assert _pool_threads() == before


def test_a_local_install_is_adopted_into_the_cluster_and_the_pool_is_closed(
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    installed_machine_identity: MachineIdentityReader,
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    skills = unit_home / "skills"
    _skill_tree(skills / "local-note", "kept on this machine")
    install_registry.register(install_registry.InstalledPackage(name="local-note", type="skill"))
    before = _pool_threads()

    adopt_local_extensions(
        database_factory=operator_database, machine=installed_machine_identity, skills_root=skills
    )

    assert capsys.readouterr().out.splitlines() == ["    adopted into the cluster: local-note"]
    adopted = cluster_registry.get(db_conn, "local-note")
    assert adopted is not None and adopted.source == "local:unit-test-machine"
    assert _pool_threads() == before


def test_an_unreadable_local_registry_stops_the_adoption_and_the_pool_is_closed(
    db_url: str,
    operator_database: OperatorDatabaseFactory,
    installed_machine_identity: MachineIdentityReader,
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (unit_home / "installed.json").write_text("{ this is not json")
    before = _pool_threads()

    adopt_local_extensions(
        database_factory=operator_database,
        machine=installed_machine_identity,
        skills_root=unit_home / "skills",
    )

    assert "could not adopt local installs" in capsys.readouterr().err
    assert _pool_threads() == before
