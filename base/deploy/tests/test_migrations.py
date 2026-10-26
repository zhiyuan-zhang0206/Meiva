from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import (
    BASELINE_NAME,
    CodeBehindSchema,
    MigrationLayoutError,
    SchemaVersionMismatch,
    applied_migration_names,
    apply_pending_migrations,
    check_schema_version,
    list_migration_files,
    required_migration_set,
    schema_mutation_lock,
)
from base.deploy.schema.tests.migration_support import (
    SYN,
    SYN2,
    init_repo,
    set_table_to,
    try_lock_from_other_conn,
)
from base.deploy.schema.tests.migration_support import (
    reset_schema_migrations_state as reset_schema_migrations_state,
)
from base.paths.home import resolve_meiva_home


def test_required_set_is_just_baseline_with_empty_migrations(tmp_path: Path) -> None:
    init_repo(tmp_path)
    assert required_migration_set(migrations_dir=tmp_path) == {BASELINE_NAME}


def test_list_migration_files_empty_is_valid(tmp_path: Path) -> None:
    init_repo(tmp_path)
    assert list_migration_files(migrations_dir=tmp_path) == []


def test_applied_migration_names_reads_the_set(db_conn: psycopg.Connection) -> None:
    assert applied_migration_names(db_conn) == {BASELINE_NAME}


def test_check_passes_when_aligned(db_conn: psycopg.Connection, tmp_path: Path) -> None:
    init_repo(tmp_path)
    check_schema_version(db_conn, migrations_dir=tmp_path)


def test_check_raises_when_db_behind(db_conn: psycopg.Connection) -> None:
    set_table_to(db_conn, "set", [])
    with pytest.raises(SchemaVersionMismatch) as ei:
        check_schema_version(db_conn, migrations_dir=MIGRATIONS_DIR)
    assert BASELINE_NAME in str(ei.value)


def test_check_raises_when_code_behind(db_conn: psycopg.Connection) -> None:
    set_table_to(db_conn, "set", [BASELINE_NAME, SYN])
    with pytest.raises(CodeBehindSchema) as ei:
        check_schema_version(db_conn, migrations_dir=MIGRATIONS_DIR)
    assert SYN in str(ei.value)


def test_apply_pending_nothing_when_baselined(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    _ = db_conn
    init_repo(tmp_path)
    with psycopg.connect(settings.data_plane.db_url) as fresh:
        assert (
            apply_pending_migrations(
                fresh,
                machine_name=machine_identity.name,
                migrations_dir=tmp_path,
                home=resolve_meiva_home(),
            )
            == []
        )


def test_apply_pending_applies_post_baseline(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    _ = db_conn
    hour = tmp_path / "2999/12/31/23"
    hour.mkdir(parents=True)
    (hour / f"{SYN}.sql").write_text("CREATE TABLE syn_t (id int);")
    init_repo(tmp_path)
    try:
        with psycopg.connect(settings.data_plane.db_url) as fresh:
            assert apply_pending_migrations(
                fresh,
                machine_name=machine_identity.name,
                migrations_dir=tmp_path,
                home=resolve_meiva_home(),
            ) == [SYN]
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(BASELINE_NAME,), (SYN,)]
            cur.execute("SELECT to_regclass('syn_t')")
            row = cur.fetchone()
            assert row is not None and row[0] is not None
        with psycopg.connect(settings.data_plane.db_url) as again:
            assert (
                apply_pending_migrations(
                    again,
                    machine_name=machine_identity.name,
                    migrations_dir=tmp_path,
                    home=resolve_meiva_home(),
                )
                == []
            )
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS syn_t")


def test_untracked_migration_is_skipped_and_warned(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    loguru_records: list[dict[str, Any]],
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    _ = db_conn
    (tmp_path / f"{SYN}.sql").write_text("CREATE TABLE syn_ok_t (id int);")
    init_repo(tmp_path)
    rogue = "29991231T235958_synthetic-rogue"
    (tmp_path / f"{rogue}.sql").write_text("CREATE TABLE syn_rogue_t (id int);")
    try:
        with psycopg.connect(settings.data_plane.db_url) as fresh:
            assert apply_pending_migrations(
                fresh,
                machine_name=machine_identity.name,
                migrations_dir=tmp_path,
                home=resolve_meiva_home(),
            ) == [SYN]
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            row = c.execute("SELECT to_regclass('syn_rogue_t')").fetchone()
            assert row is None or row[0] is None
            row = c.execute("SELECT to_regclass('syn_ok_t')").fetchone()
            assert row is not None and row[0] is not None
        assert any("untracked" in r["message"] and rogue in r["message"] for r in loguru_records), (
            "the skip must be loud"
        )
    finally:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS syn_ok_t")
            c.execute("DROP TABLE IF EXISTS syn_rogue_t")


def test_untracked_file_excluded_from_required_set(tmp_path: Path) -> None:
    (tmp_path / f"{SYN}.sql").write_text("-- noop")
    init_repo(tmp_path)
    (tmp_path / f"{SYN2}.sql").write_text("-- noop")
    assert required_migration_set(migrations_dir=tmp_path) == {BASELINE_NAME, SYN}


def test_untracked_malformed_name_is_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / f"{SYN}.sql").write_text("-- noop")
    init_repo(tmp_path)
    (tmp_path / "0001_legacy.sql").write_text("-- noop")
    assert [n for n, _ in list_migration_files(migrations_dir=tmp_path)] == [SYN]


def test_non_git_dir_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(MigrationLayoutError, match="git worktree"):
        list_migration_files(migrations_dir=tmp_path)


def test_apply_multi_statement_migration_over_prepared_conn(
    db_conn: None,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    (tmp_path / f"{SYN}.sql").write_text(
        "CREATE TABLE mstest_t (id int, note text);\nINSERT INTO mstest_t (id, note) VALUES (1, 'ok');"
    )
    init_repo(tmp_path)

    with psycopg.connect(settings.data_plane.db_url, prepare_threshold=0) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS mstest_t")
        conn.commit()
        applied = apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=tmp_path,
            home=resolve_meiva_home(),
        )
        assert SYN in applied
        with conn.cursor() as cur:
            cur.execute("SELECT id, note FROM mstest_t")
            assert cur.fetchone() == (1, "ok")
        with conn.cursor() as cur:
            cur.execute("DROP TABLE mstest_t")
        conn.commit()


def test_schema_mutation_lock_is_exclusive(db_conn: psycopg.Connection) -> None:
    assert try_lock_from_other_conn() is True
    with schema_mutation_lock(db_conn):
        assert try_lock_from_other_conn() is False
    assert try_lock_from_other_conn() is True
    db_conn.rollback()


def test_schema_mutation_lock_released_on_exception(db_conn: psycopg.Connection) -> None:
    with pytest.raises(RuntimeError, match="boom"), schema_mutation_lock(db_conn):
        raise RuntimeError("boom")
    assert try_lock_from_other_conn() is True
    db_conn.rollback()


# meiva:note
# Verify the folded baseline schema defaults relay custody fields and populates terminal notice snapshots on expiry.
def test_transport_delta_preserves_legacy_custody_and_does_not_backfill_notices(
    db_conn: psycopg.Connection,
) -> None:
    with db_conn.transaction():
        row = db_conn.execute(
            "INSERT INTO agents(label) VALUES('transport fixture') RETURNING id"
        ).fetchone()
        assert row is not None
        aid = row[0]
        db_conn.execute(
            "INSERT INTO agents_meta(id,status,machine,harness) VALUES(%s,'idling','fixture','meiva')",
            (aid,),
        )
        db_conn.execute(
            "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,ttl_seconds,"
            "expires_at,ended_at,handoff_applied_at) "
            "VALUES(gen_random_uuid(),%s,'codex','fixture','released',3600,"
            "now()+interval '1 hour',now(),now()),"
            "(gen_random_uuid(),%s,'codex','fixture','active',3600,"
            "now()+interval '1 hour',NULL,NULL)",
            (aid, aid),
        )
        assert db_conn.execute(
            "SELECT relay_generation,relay_identity,terminal_notice_pending_at "
            "FROM agent_impersonations WHERE agent_id=%s ORDER BY session_id",
            (aid,),
        ).fetchall() == [(0, None, None), (0, None, None)]
        db_conn.execute(
            "UPDATE agent_impersonations SET status='expired',ended_at=now() "
            "WHERE agent_id=%s AND status='active'",
            (aid,),
        )
        assert db_conn.execute(
            "SELECT terminal_notice_pending_at IS NOT NULL,terminal_notice_snapshot->>'status' "
            "FROM agent_impersonations WHERE agent_id=%s AND status='expired'",
            (aid,),
        ).fetchone() == (True, "expired")
    db_conn.rollback()
