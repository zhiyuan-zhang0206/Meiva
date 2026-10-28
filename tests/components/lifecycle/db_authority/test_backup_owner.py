"""Logical backup maintenance on an always-authenticated born home.

A home born by the real start steps (`test_single_box.born`): NOLOGIN schema
owner, write generation 0, a credential-free `MEIVA_DB_URL`. The maintenance
process here holds NO write-generation login: its `MEIVA_DB_URL` is the
credential-free endpoint and no `MEIVA_DB_ADMIN_PASSWORD` exists anywhere. The
scheduled dump must still work, because it dials the home's own authority
(`base.cluster.dataplane.pg_admin`): the OS-user administrator over the owner-only socket,
acting as the schema owner.
"""

from __future__ import annotations

import getpass
import os
import shutil
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.conninfo import conninfo_to_dict

from base.cluster.dataplane import pooler
from base.cluster.dataplane.pg_admin import pg_socket_dir
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.config import ConfigBoot, get_field, set_field
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.db_authority import delivery
from base.host.net.url_secret import url_with_port
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.commands.tests.test_single_box import Born
from cli.commands.tests.test_single_box import born as born
from cli.commands.tests.test_single_box import configured as configured
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.backup.layout import BackupTree
from services.backup.scheduler.settings import DumpSettings
from tests.path_scoped.cli_tests import operator_database as operator_database

pytestmark = pytest.mark.skipif(
    not (Path(pooler.pgbouncer_bin()).exists() or shutil.which(pooler.pgbouncer_bin())),
    reason="pgbouncer not installed (brew/apt)",
)


@pytest.fixture
def maintenance(born: Born) -> Generator[Born]:
    """The born home, seen by a process that holds no write-generation login."""
    endpoint = born.endpoint()
    assert conninfo_to_dict(endpoint).get("password") is None
    assert "MEIVA_DB_ADMIN_PASSWORD" not in (born.home / ".env").read_text()
    previous = get_field("db_url")
    set_field("db_url", endpoint)
    try:
        yield born
    finally:
        set_field("db_url", previous)


def _seed_conversation(born: Born) -> int:
    """One agent with a readable checkpoint conversation, written as the gateway."""
    gateway = url_with_port(born.dsn("gateway"), born.pg_port)
    with psycopg.connect(gateway, autocommit=True) as conn:
        row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = datetime.now(UTC).isoformat()
    checkpoint["channel_values"] = {"messages": [HumanMessage(content="backed up")]}
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(gateway) as saver:
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={"messages": "1"},
        )
    return agent_id


def _assert_owner_dial(conninfo: str) -> None:
    """The home's administrator over its owner-only socket, acting as the owner."""
    target = conninfo_to_dict(conninfo)
    assert target["host"] == str(pg_socket_dir(home=meiva_home()))
    assert target["user"] == getpass.getuser()
    assert target["dbname"] == "meiva"
    assert target["options"] == "-c role=meiva"
    assert "password" not in target


def test_scheduled_backup_dumps_as_the_owner_and_restores(
    maintenance: Born,
    tmp_path: Path,
    throwaway_config: ThrowawayConfig,
) -> None:
    from services.backup import dump, restore_drill
    from services.backup.dump import pg_dump_env
    from services.backup.scheduler import worker

    agent_id = _seed_conversation(maintenance)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    # The scheduled worker's own operation, then the controller's publication.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (delivery.GENERATION_ENV, "MEIVA_DB_ADMIN_PASSWORD")
    } | {"MEIVA_DB_URL": maintenance.endpoint()}
    config = ConfigBoot(environment=env)
    version = CodeVersion(LoadedCommit.capture())
    gate = ProcessDbGate(version=version.get, process="backup-test")

    def database() -> Database:
        return Database(db_config_from_boot(config), gate=gate)

    def database_for_url(url: str) -> Database:
        return Database(replace(db_config_from_boot(config), db_url=url), gate=gate)

    tree = BackupTree(maintenance.home)
    settings = DumpSettings.read(config)
    source = dump.dump_source(
        database(), is_remote=settings.is_remote, plane_url=settings.plane_url, home=meiva_home()
    )
    conninfo, password = dump.passwordless_conninfo(source)
    _assert_owner_dial(conninfo)
    assert pg_dump_env(password) == {}  # no PGPASSWORD: the dial carries no credential at all
    result = worker.execute(
        {"kind": "dump", "now": datetime.now(UTC).isoformat()},
        work,
        settings=settings,
        tree=tree,
        database=database,
        database_for_url=database_for_url,
        env=dict(os.environ),
    )
    artifact = worker.commit_scheduled_backup(
        work / "artifact" / str(result["artifact"]),
        str(result["sha256"]),
        tree=tree,
        keep=settings.keep,
    )

    assert artifact.parent == maintenance.home / "backups" / "db"

    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    report, _elapsed = restore_drill.run_drill(
        artifact,
        tree=tree,
        database_for_url=database_for_url,
        foreground=True,
        scratch_root=scratch,
        throwaway=throwaway_config,
        env=dict(os.environ),
    )
    assert report.agents == 1
    assert report.sample_agent_id == agent_id
    assert report.sample_message_count == 1
    assert report.agents_owner == "meiva"
