# meiva:note
# Contract tests for the standalone encrypted-backup restore drill.

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import ormsgpack
import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import EXT_PYDANTIC_V2

from base.agents.history.checkpoint_serde import RetiredCheckpointType
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
    # meiva:note
    # The drill's verification is stronger than a successful pg_restore: it reads every checkpoint
    # table and loads one restored conversation through the production checkpoint reader.
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


def _envelope(module: str, name: str, kwargs: dict[str, object] | None = None) -> bytes:
    # meiva:note
    # One pydantic-v2 ext envelope, as the serializer writes a pydantic object.
    return ormsgpack.packb(
        ormsgpack.Ext(
            EXT_PYDANTIC_V2,
            ormsgpack.packb(
                (module, name, {"value": 1} if kwargs is None else kwargs, "model_validate_json")
            ),
        )
    )


def _store_channel(db_conn: psycopg.Connection, agent_id: int, channel: str, blob: bytes) -> None:
    # meiva:note
    # Give the thread's newest checkpoint one more stored channel, bypassing the writer.
    #
    # The point of the census is what is *stored*: a class the writer no longer has cannot be
    # produced through the saver, so a poisoned row is written directly.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE checkpoints "
            "SET checkpoint = jsonb_set("
            "checkpoint, ARRAY['channel_versions', %s::text], to_jsonb('1'::text)) "
            "WHERE thread_id = %s",
            (channel, str(agent_id)),
        )
        cur.execute(
            "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (%s, '', %s, '1', 'msgpack', %s)",
            (str(agent_id), channel, blob),
        )
    db_conn.commit()


def _verify() -> restore_drill.RestoreReport:
    return restore_drill.verify_restored_database(
        settings.data_plane.db_url, database_for_url=_database_factory()
    )


def test_the_census_names_the_classes_a_restored_conversation_holds(
    db_conn: psycopg.Connection,
) -> None:
    # meiva:note
    # A green drill now carries its denominator: which classes it saw, over how many conversations,
    # and that none of them was unknown.
    _seed_conversation(db_conn)

    report = _verify()

    assert report.census.threads >= 1
    assert report.census.blobs + report.census.writes >= 1
    assert report.census.unknown == ()
    assert report.census.failure() is None
    assert "langchain_core.messages.human.HumanMessage" in report.census.types


def test_the_census_refuses_a_class_nothing_accounts_for(
    db_conn: psycopg.Connection,
) -> None:
    # meiva:note
    # The failure the drill could not see before: the serializer answers an unregistered envelope
    # with the raw dict and a warning, so every other check stays green.
    agent_id = _seed_conversation(db_conn)
    _store_channel(db_conn, agent_id, "weird", _envelope("no.such.module", "GhostType"))

    with pytest.raises(RuntimeError, match="neither load nor account for"):
        _verify()


def test_the_census_names_a_retired_class_it_cannot_build(
    db_conn: psycopg.Connection,
) -> None:
    # meiva:note
    # A class the code deleted is historical residue, not a fault: the reader answers the envelope
    # with the raw payload, and the record counts the name so a reader of the record knows what the
    # green covers.
    agent_id = _seed_conversation(db_conn)
    _store_channel(
        db_conn,
        agent_id,
        "native_work",
        _envelope("base.agents.incarnation.native_work_models", "NativeWorkTarget"),
    )

    report = _verify()

    assert report.census.retired == ("base.agents.incarnation.native_work_models.NativeWorkTarget",)
    assert report.census.revived == ()
    assert report.census.failure() is None


def test_the_census_refuses_a_retired_class_the_reader_still_builds(
    db_conn: psycopg.Connection,
) -> None:
    # meiva:note
    # The registry saying "deleted" while the reader still resolves the class is the fault the
    # census exists for: the class came back, or the registry is stale. Judged through the reader's
    # own serializer, so an allowlisted class counts even though the drill never imports it.
    entry = RetiredCheckpointType(
        module="agent.state_channels",
        name="AttachState",
        retired_in="0000000000",
        retired_at=datetime(2026, 1, 1, tzinfo=UTC),
        note="a class this build still has",
    )
    agent_id = _seed_conversation(db_conn)
    _store_channel(
        db_conn,
        agent_id,
        "attach",
        _envelope("agent.state_channels", "AttachState"),
    )

    census = restore_drill.census_checkpoint_types(db_conn, retired=(entry,))

    assert census.retired == ("agent.state_channels.AttachState",)
    assert census.revived == ("agent.state_channels.AttachState",)
    refusal = census.failure()
    assert refusal is not None
    assert "importable in this build" in refusal


def test_the_census_refuses_a_retired_class_the_build_can_still_build(
    db_conn: psycopg.Connection,
) -> None:
    # meiva:note
    # A class that came back is a fault even before anyone allowlists it: the writer can produce it
    # again while the reader silently degrades to the raw payload. Asked of the serializer — decode
    # the row with the class allowed and see whether it built anything.
    entry = RetiredCheckpointType(
        module="json",
        name="JSONDecoder",
        retired_in="0000000000",
        retired_at=datetime(2026, 1, 1, tzinfo=UTC),
        note="a stdlib class this build trivially has",
    )
    agent_id = _seed_conversation(db_conn)
    _store_channel(db_conn, agent_id, "decoder", _envelope("json", "JSONDecoder", {}))

    census = restore_drill.census_checkpoint_types(db_conn, retired=(entry,))

    assert census.revived == ("json.JSONDecoder",)
    refusal = census.failure()
    assert refusal is not None
    assert "importable in this build" in refusal


def test_the_census_does_not_green_an_empty_sample(db_conn: psycopg.Connection) -> None:
    # meiva:note
    # A zero denominator is not a pass: a store with no conversation proves nothing.
    census = restore_drill.census_checkpoint_types(db_conn)

    assert census.threads == 0
    refusal = census.failure()
    assert refusal is not None
    assert "no checkpoint to census" in refusal


def _grant_prod_roles(db_conn: psycopg.Connection) -> None:
    # meiva:note
    # Reproduce the prod role set in the test DB so the dump carries the GRANT/OWNER statements that
    # broke production drills (the throwaway cluster only has `meiva`, so pg_restore hit `role does
    # not exist`): the 2026-08-27 missing roles, plus the 2026-09-21 admin-owned-object variant — a
    # `zzy`-owned ad-hoc table, as the sweep backup convention leaves behind.
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
    # meiva:note
    # The command path decrypts, restores, and proves a checkpoint reader can consume the restored
    # conversation without touching the source DB.
    #
    # The test DB carries the prod role set (meiva_main / meiva_runner / grafana_ro / zzy with
    # grants, plus a zzy-owned ad-hoc table) so the dump exercises the same restore path that failed
    # in production on 2026-08-27 and 2026-09-21.
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
        datetime.now(UTC),
        tree=tree,
        db_url=db_url,
        keep=7,
        offsite_target=None,
        env=dict(os.environ),
    )

    report, elapsed = restore_drill.run_drill(
        artifact,
        tree=tree,
        database_for_url=_database_factory(),
        throwaway=throwaway_config,
        env=dict(os.environ),
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
        env=dict(os.environ),
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
    # meiva:note
    # The base is picked against a multiple of the decrypted dump's size: measured restores ran
    # ~1.7x (10.16 GiB artifact, 2026-09-14) and >=2.7x (3.0 GiB artifact, 2026-09-19), so the drill
    # reserves 3x as the floor for choosing a base.
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
        restore_drill.restore_dump(
            garbage, db_url, base=tmp_path, home=meiva_home(), env=dict(os.environ)
        )

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
