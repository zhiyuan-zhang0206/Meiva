"""Contract tests for the standalone encrypted-backup restore drill."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from base.cluster.authority import ensure_groups
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_settings
from base.host.db_authority.model import GATEWAY_GROUP, RUNNER_GROUP, Groups
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from services.backup import dump as backup
from services.backup import restore_drill
from services.backup.layout import BackupTree
from services.backup.tests.support import minted_tree


def _database_factory() -> Callable[[str], Database]:
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="backup-test")

    def for_url(url: str) -> Database:
        return Database(replace(db_config_from_settings(), db_url=url), gate=gate)

    return for_url


def _write_checkpoint(agent_id: int) -> None:
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = datetime.now(UTC).isoformat()  # real conversations carry a ts
    checkpoint["channel_values"] = {"messages": [HumanMessage(content="restored conversation")]}
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={"messages": "1"},
        )


def test_verification_reports_schema_counts_and_readable_conversation(
    db_conn: psycopg.Connection,
) -> None:
    """The drill's verification is stronger than a successful pg_restore: it
    reads every checkpoint table and loads one restored conversation through
    the production checkpoint reader."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
    assert row is not None
    agent_id = row[0]
    db_conn.commit()
    _write_checkpoint(agent_id)

    report = restore_drill.verify_restored_database(
        settings.data_plane.db_url, database_for_url=_database_factory()
    )

    assert report.agents == 1
    assert report.checkpoints >= 1
    assert report.checkpoint_blobs >= 0
    assert report.checkpoint_writes >= 0
    assert report.sample_agent_id == agent_id
    assert report.sample_message_count == 1


def _grant_prod_roles(db_conn: psycopg.Connection) -> None:
    """Reproduce the prod role set in the test DB so the dump carries the
    GRANT/OWNER statements that broke production drills (the throwaway cluster
    only has `meiva`, so pg_restore hit `role does not exist`): the 2026-08-27
    missing roles, plus the 2026-09-21 admin-owned-object variant — a
    `zzy`-owned ad-hoc table, as the sweep backup convention leaves behind."""
    from psycopg import sql as pgsql

    # This is the session database: capability groups must retain their formal
    # NOLOGIN contract when later tests use authenticated runner logins.
    with psycopg.connect(db_conn.info.dsn, autocommit=True) as admin:
        ensure_groups(
            admin,
            owner=db_conn.info.user,
            database=db_conn.info.dbname,
            groups=Groups(gateway=GATEWAY_GROUP, runner=RUNNER_GROUP),
        )
    with db_conn.cursor() as cur:
        for role in restore_drill.RESTORE_ROLES:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                cur.execute(pgsql.SQL("CREATE ROLE {} LOGIN").format(pgsql.Identifier(role)))
        cur.execute("GRANT SELECT ON agents TO meiva_runner")
        cur.execute("GRANT SELECT ON agents TO grafana_ro")
        cur.execute(
            pgsql.SQL("ALTER TABLE agents OWNER TO {}").format(pgsql.Identifier("meiva_main"))
        )
        # The 2026-09-21 variant: an ad-hoc table owned by the admin role
        # (the model_sweep_backup_* convention leaves one in the app database).
        cur.execute("CREATE TABLE IF NOT EXISTS adhoc_admin_owned (id int)")
        cur.execute(
            pgsql.SQL("ALTER TABLE adhoc_admin_owned OWNER TO {}").format(pgsql.Identifier("zzy"))
        )
    db_conn.commit()


def _seed_conversation(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
    assert row is not None
    agent_id = row[0]
    db_conn.commit()
    _write_checkpoint(agent_id)
    return agent_id


@pytest.mark.skipif(
    not restore_drill.has_pg_tool("pg_dump", home=meiva_home()), reason="needs native pg_dump"
)
def test_run_drill_restores_an_encrypted_artifact_into_throwaway_postgres(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    db_url: str,
    throwaway_config: ThrowawayConfig,
) -> None:
    """The command path decrypts, restores, and proves a checkpoint
    reader can consume the restored conversation without touching the source DB.

    The test DB carries the prod role set (meiva_main / meiva_runner / grafana_ro /
    zzy with grants, plus a zzy-owned ad-hoc table) so the dump exercises the
    same restore path that failed in production on 2026-08-27 and 2026-09-21."""
    _grant_prod_roles(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT rolname, rolcanlogin FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
            ([GATEWAY_GROUP, RUNNER_GROUP],),
        )
        assert cur.fetchall() == [(GATEWAY_GROUP, False), (RUNNER_GROUP, False)]
    agent_id = _seed_conversation(db_conn)
    # meiva:note
    # The session database is not a born home: dump it through an explicit dial, into a
    # home of this test's own, under the passphrase a gateway birth pins there.
    tree = minted_tree(tmp_path)
    artifact = backup.run_backup(
        datetime.now(UTC), tree=tree, db_url=db_url, keep=7, offsite_target=None
    )

    report, elapsed = restore_drill.run_drill(
        artifact, tree=tree, database_for_url=_database_factory(), throwaway=throwaway_config
    )

    assert report.agents == 1
    assert report.sample_agent_id == agent_id
    assert report.sample_message_count == 1
    assert report.agents_owner == "meiva_main"
    assert elapsed > 0


# meiva:note
# `python -m services.backup.restore_drill ARTIFACT` reads the process home's passphrase,
# restores into throwaway Postgres and prints one result line.
def test_the_command_restores_an_artifact_and_prints_its_verdict(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    db_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_id = _seed_conversation(db_conn)
    tree = BackupTree(meiva_home())
    artifact = backup.run_backup(
        datetime.now(UTC),
        tree=tree,
        db_url=db_url,
        keep=7,
        offsite_target=None,
        staging=tmp_path / "staged",
    )

    restore_drill.main([str(artifact)])

    out = capsys.readouterr().out
    assert "restore drill passed" in out
    assert f"sample_agent={agent_id}" in out
    assert "messages=1" in out


def test_the_newest_managed_artifact_is_the_default_target(tmp_path: Path) -> None:
    tree = BackupTree(tmp_path)
    tree.managed.mkdir(parents=True)
    (tree.managed / "meiva-20260601T100000Z.dump.enc").write_bytes(b"older")
    newest = tree.managed / "meiva-20260602T100000Z.dump.enc"
    newest.write_bytes(b"newer")
    (tree.managed / "notes.txt").write_bytes(b"foreign")

    assert restore_drill.newest_artifact(tree) == newest


def test_with_no_managed_artifact_the_default_target_is_a_fault(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no managed backup artifact"):
        restore_drill.newest_artifact(BackupTree(tmp_path))


def test_scratch_space_requirement_scales_with_the_dump_size(tmp_path: Path) -> None:
    """The base is picked against a multiple of the decrypted dump's size: measured
    restores ran ~1.7x (10.16 GiB artifact, 2026-09-14) and >=2.7x (3.0 GiB
    artifact, 2026-09-19), so the drill reserves 3x as the floor for choosing a
    base."""
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"x" * 1000)
    assert restore_drill.scratch_space_requirement(dump) == 3000


# meiva:note
# A restore that fails must carry the diagnosis, not a bare exit code (the 2026-09-14
# failure): the message names the base, its free space and the knob that moves it.
def test_a_dump_pg_restore_cannot_read_fails_with_the_base_and_the_capacity_knob(
    tmp_path: Path, db_url: str
) -> None:
    garbage = tmp_path / "backup.dump"
    garbage.write_bytes(b"this is not a custom-format archive")

    with pytest.raises(RuntimeError) as failure:
        restore_drill.restore_dump(garbage, db_url, base=tmp_path, home=meiva_home())

    message = str(failure.value)
    assert "pg_restore exited" in message
    assert str(tmp_path) in message
    assert " free)" in message
    assert "MEIVA_PG_THROWAWAY_BASE" in message


def test_a_retired_option_is_rejected_by_the_parser(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        restore_drill.main(["--legacy-empty-secret-passphrase"])

    assert error.value.code == 2
    assert "--legacy-empty-secret-passphrase" in capsys.readouterr().err
