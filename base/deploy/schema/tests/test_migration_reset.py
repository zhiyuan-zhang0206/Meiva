from __future__ import annotations

from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import (
    BASELINE_NAME,
    apply_pending_migrations,
)
from base.deploy.schema.tests.migration_support import (
    SYN,
    SYN_ORPHAN,
    db_url,
    init_repo,
)
from base.deploy.schema.tests.migration_support import (
    reset_schema_migrations_state as reset_schema_migrations_state,
)
from base.paths.home import resolve_meiva_home


def test_apply_pending_squashes_orphaned_applied_names(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    _ = db_conn
    orphan = "20260815T000001_synthetic-orphan"
    init_repo(tmp_path)
    with psycopg.connect(db_url(), autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (orphan,))
    try:
        with psycopg.connect(db_url()) as fresh:
            assert (
                apply_pending_migrations(
                    fresh,
                    machine_name=machine_identity.name,
                    migrations_dir=tmp_path,
                    home=resolve_meiva_home(),
                )
                == []
            )
        with psycopg.connect(db_url(), autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(BASELINE_NAME,)]
    finally:
        with psycopg.connect(db_url(), autocommit=True) as c:
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (orphan,))


def test_apply_pending_squash_then_apply_pending(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    _ = db_conn
    orphan = "20260815T000001_synthetic-orphan"
    (tmp_path / f"{SYN}.sql").write_text("CREATE TABLE syn_squash_t (id int);")
    init_repo(tmp_path)
    with psycopg.connect(db_url(), autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (orphan,))
    try:
        with psycopg.connect(db_url()) as fresh:
            assert apply_pending_migrations(
                fresh,
                machine_name=machine_identity.name,
                migrations_dir=tmp_path,
                home=resolve_meiva_home(),
            ) == [SYN]
        with psycopg.connect(db_url(), autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(BASELINE_NAME,), (SYN,)]
            cur.execute("SELECT to_regclass('syn_squash_t')")
            row = cur.fetchone()
            assert row is not None and row[0] is not None
    finally:
        with psycopg.connect(db_url(), autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS syn_squash_t")
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (orphan,))


def test_squash_does_not_touch_baseline_or_pending(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    _ = db_conn
    (tmp_path / f"{SYN}.sql").write_text("CREATE TABLE syn_keep_t (id int);")
    init_repo(tmp_path)
    with psycopg.connect(db_url(), autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (SYN,))
    try:
        with psycopg.connect(db_url()) as fresh:
            assert (
                apply_pending_migrations(
                    fresh,
                    machine_name=machine_identity.name,
                    migrations_dir=tmp_path,
                    home=resolve_meiva_home(),
                )
                == []
            )
        with psycopg.connect(db_url(), autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(BASELINE_NAME,), (SYN,)]
    finally:
        with psycopg.connect(db_url(), autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS syn_keep_t")
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (SYN,))


def test_squash_authority_checked_even_without_pending(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    _ = db_conn
    orphan = "20260815T000001_synthetic-orphan"
    init_repo(tmp_path)
    with psycopg.connect(db_url(), autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (orphan,))
        c.execute(
            "INSERT INTO machine_units (machine_name, home, serve_gateway) "
            "VALUES ('real-gateway', '/real/home', true)"
        )
    try:
        from base.deploy.schema.migrations import MigrationAuthorityMismatch

        with psycopg.connect(db_url()) as fresh:
            try:
                apply_pending_migrations(
                    fresh,
                    machine_name=machine_identity.name,
                    migrations_dir=tmp_path,
                    home=tmp_path / "not-the-gateways-home",
                )
            except MigrationAuthorityMismatch:
                pass
            else:
                raise AssertionError("squash without authority must be refused")
    finally:
        with psycopg.connect(db_url(), autocommit=True) as c:
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (orphan,))
            c.execute("DELETE FROM machine_units WHERE machine_name = 'real-gateway'")


def test_squash_logs_the_converged_names(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    loguru_records: list[dict[str, Any]],
    machine_identity: MachineIdentityReader,
) -> None:
    _ = db_conn
    orphan = "20260815T000001_synthetic-orphan"
    init_repo(tmp_path)
    with psycopg.connect(db_url(), autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (orphan,))
    try:
        with psycopg.connect(db_url()) as fresh:
            apply_pending_migrations(
                fresh,
                machine_name=machine_identity.name,
                migrations_dir=tmp_path,
                home=resolve_meiva_home(),
            )
        assert any("squash" in r["message"] and orphan in r["message"] for r in loguru_records), (
            "the squash must be loud"
        )
    finally:
        with psycopg.connect(db_url(), autocommit=True) as c:
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (orphan,))


def test_squash_refuses_partial_pre_reset_history(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    from base.deploy.schema.migrations import V010_PRE_RESET_SET, MigrationHistoryGap

    assert len(V010_PRE_RESET_SET) == 59, "frozen set drifted"
    partial = sorted(V010_PRE_RESET_SET)[:24]
    init_repo(tmp_path)
    with psycopg.connect(db_url(), autocommit=True) as c:
        for name in partial:
            c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    try:
        with (
            psycopg.connect(db_url()) as fresh,
            pytest.raises(MigrationHistoryGap),
        ):
            apply_pending_migrations(
                fresh,
                machine_name=machine_identity.name,
                migrations_dir=tmp_path,
                home=resolve_meiva_home(),
            )
        with psycopg.connect(db_url(), autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            surviving = cur.fetchone()
            assert surviving is not None and surviving[0] == 1 + len(partial)
    finally:
        with psycopg.connect(db_url(), autocommit=True) as c:
            for name in partial:
                c.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))


def test_squash_converges_full_pre_reset_history(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    from base.deploy.schema.migrations import V010_PRE_RESET_SET

    all_names = sorted(V010_PRE_RESET_SET)
    init_repo(tmp_path)
    with psycopg.connect(db_url(), autocommit=True) as c:
        for name in all_names:
            c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    try:
        with psycopg.connect(db_url()) as fresh:
            assert (
                apply_pending_migrations(
                    fresh,
                    machine_name=machine_identity.name,
                    migrations_dir=tmp_path,
                    home=resolve_meiva_home(),
                )
                == []
            )
        with psycopg.connect(db_url(), autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(BASELINE_NAME,)]
    finally:
        with psycopg.connect(db_url(), autocommit=True) as c:
            for name in all_names:
                c.execute("DELETE FROM schema_migrations WHERE name = %s", (name,))


def test_squash_ignores_pre_reset_names_outside_the_frozen_set(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    _ = db_conn
    init_repo(tmp_path)
    with psycopg.connect(db_url(), autocommit=True) as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (SYN_ORPHAN,))
    try:
        with psycopg.connect(db_url()) as fresh:
            assert (
                apply_pending_migrations(
                    fresh,
                    machine_name=machine_identity.name,
                    migrations_dir=tmp_path,
                    home=resolve_meiva_home(),
                )
                == []
            )
        with psycopg.connect(db_url(), autocommit=True) as v, v.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            assert cur.fetchall() == [(BASELINE_NAME,)]
    finally:
        with psycopg.connect(db_url(), autocommit=True) as c:
            c.execute("DELETE FROM schema_migrations WHERE name = %s", (SYN_ORPHAN,))


# meiva:note
# 7 is absent on purpose. That shape used to refuse and now converges, because the
# one name it is missing still has a file; dropping it from this list without a word
# would read as "7 was never tested". It is covered by
# `test_current_reset_converges_when_the_missing_names_still_have_files` below (#8150).
@pytest.mark.parametrize("applied_count", [0, 1])
def test_current_reset_refuses_missing_history_without_mutation(
    db_conn: psycopg.Connection,
    applied_count: int,
    machine_identity: MachineIdentityReader,
) -> None:
    from base.deploy.schema.migration_history import PRE_RESET_SET
    from base.deploy.schema.migrations import MigrationHistoryGap, applied_migration_names
    from base.deploy.schema.tests.migration_support import set_table_to

    assert len(PRE_RESET_SET) == 8
    before = {BASELINE_NAME, *sorted(PRE_RESET_SET)[:applied_count]}
    set_table_to(db_conn, "set", before)
    with psycopg.connect(db_url()) as conn:
        with pytest.raises(MigrationHistoryGap, match="2026-11-01"):
            apply_pending_migrations(
                conn,
                machine_name=machine_identity.name,
                migrations_dir=MIGRATIONS_DIR,
                home=resolve_meiva_home(),
            )
        assert applied_migration_names(conn) == before


def test_current_reset_converges_when_the_missing_names_still_have_files(
    db_url: str,
    machine_identity: MachineIdentityReader,
) -> None:
    # meiva:note
    # The 2026-11-01 reset folded eight deltas and deleted their files; three were
    # re-issued as post-baseline files because the running databases had never
    # applied them. A name that still has a file is pending work, not a gap: this
    # run applies it. Counting it as history instead would refuse here, and would
    # also refuse every later start (crossing keeps the file-backed names and
    # squashes the rest, so the "partial generation" would never clear).
    from base.deploy.schema.migration_history import PRE_RESET_SET, RESET_ANCHOR
    from base.deploy.schema.migrations import applied_migration_names
    from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database

    _ = db_url
    file_backed = {
        "20261101T050900_unified-file-upload",
        "20261101T081023_harness-switch-and-external-messages",
        "20261101T091332_retire-display-and-timeline-settings",
    }
    assert file_backed <= PRE_RESET_SET
    before = {BASELINE_NAME, *sorted(PRE_RESET_SET)[:7]}
    with throwaway_database("reset_partial_files") as url:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            c.execute("DELETE FROM schema_migrations")
            for name in sorted(before):
                c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
        with psycopg.connect(url) as conn:
            applied = apply_pending_migrations(
                conn,
                machine_name=machine_identity.name,
                migrations_dir=MIGRATIONS_DIR,
                home=resolve_meiva_home(),
            )
            assert RESET_ANCHOR in applied
            after = applied_migration_names(conn)
            assert after & file_backed == file_backed & before | (file_backed - before)
            assert (set(PRE_RESET_SET) - file_backed) & after == set()


def test_current_reset_converges_the_shape_the_deleted_files_left_behind(
    db_url: str,
    machine_identity: MachineIdentityReader,
) -> None:
    # meiva:note
    # The live shape on 2026-11-02: the five deltas whose files were folded away are
    # applied, the three re-issued ones are not, and the anchor is absent. Building
    # it means removing what the three create, because a database built from
    # `db/schema.sql` already carries their effects while the running databases
    # never ran them. Then the same apply the next start would run must converge
    # instead of raising.
    from base.deploy.schema.migration_history import PRE_RESET_SET, RESET_ANCHOR
    from base.deploy.schema.migrations import applied_migration_names
    from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database

    _ = db_url
    file_backed = {
        "20261101T050900_unified-file-upload",
        "20261101T081023_harness-switch-and-external-messages",
        "20261101T091332_retire-display-and-timeline-settings",
    }
    before = {BASELINE_NAME, *(set(PRE_RESET_SET) - file_backed)}
    with throwaway_database("reset_live_shape") as url:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            c.execute("DROP TABLE IF EXISTS upload_staged_files CASCADE")
            c.execute("DROP TABLE IF EXISTS agent_harness_messages CASCADE")
            c.execute(
                "ALTER TABLE upload_delivery_batches"
                " DROP COLUMN IF EXISTS protocol,"
                " DROP COLUMN IF EXISTS send_directory,"
                " DROP COLUMN IF EXISTS message"
            )
            c.execute("DELETE FROM schema_migrations")
            for name in sorted(before):
                c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
        with psycopg.connect(url) as conn:
            applied = apply_pending_migrations(
                conn,
                machine_name=machine_identity.name,
                migrations_dir=MIGRATIONS_DIR,
                home=resolve_meiva_home(),
            )
            assert set(applied) >= file_backed
            assert RESET_ANCHOR in applied
            assert conn.execute(
                "SELECT to_regclass('public.upload_staged_files'),"
                "       to_regclass('public.agent_harness_messages')"
            ).fetchone() == ("upload_staged_files", "agent_harness_messages")
            after = applied_migration_names(conn)
            assert (set(PRE_RESET_SET) - file_backed) & after == set()


def test_current_reset_converges_complete_history(
    db_conn: psycopg.Connection,
    machine_identity: MachineIdentityReader,
) -> None:
    from base.deploy.schema.migration_history import PRE_RESET_SET, RESET_ANCHOR
    from base.deploy.schema.migrations import (
        applied_migration_names,
        check_schema_version,
        required_migration_set,
    )
    from base.deploy.schema.tests.migration_support import (
        schema_sql_stamped_migration_names,
        set_table_to,
    )

    predecessor = set(PRE_RESET_SET) | (set(schema_sql_stamped_migration_names()) - {RESET_ANCHOR})
    set_table_to(db_conn, "set", predecessor)
    with psycopg.connect(db_url()) as conn:
        applied = apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=MIGRATIONS_DIR,
            home=resolve_meiva_home(),
        )
        assert RESET_ANCHOR in applied
        check_schema_version(conn, migrations_dir=MIGRATIONS_DIR)
        expected = required_migration_set(migrations_dir=MIGRATIONS_DIR)
        assert applied_migration_names(conn) == expected
        assert (
            apply_pending_migrations(
                conn,
                machine_name=machine_identity.name,
                migrations_dir=MIGRATIONS_DIR,
                home=resolve_meiva_home(),
            )
            == []
        )


@pytest.mark.parametrize("apply", [False, True])
def test_integer_history_is_refused_without_conversion(
    db_conn: psycopg.Connection,
    apply: bool,
    machine_identity: MachineIdentityReader,
) -> None:
    from base.deploy.schema.migrations import MigrationLayoutError, check_schema_version
    from base.deploy.schema.tests.migration_support import set_table_to

    set_table_to(db_conn, "legacy", range(1, 82))
    with psycopg.connect(db_url()) as conn:
        with pytest.raises(MigrationLayoutError, match="matching release"):
            apply_pending_migrations(
                conn,
                machine_name=machine_identity.name,
                migrations_dir=MIGRATIONS_DIR,
                home=resolve_meiva_home(),
            ) if apply else check_schema_version(conn, migrations_dir=MIGRATIONS_DIR)
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(version,) for version in range(1, 82)]


def test_reset_anchor_and_history_deletion_roll_back_together(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    from base.deploy.schema import migrations
    from base.deploy.schema.migration_history import PRE_RESET_SET, RESET_ANCHOR
    from base.deploy.schema.tests.migration_support import set_table_to

    (tmp_path / f"{RESET_ANCHOR}.sql").write_text("SELECT 1;")
    init_repo(tmp_path)
    predecessor = {BASELINE_NAME, *PRE_RESET_SET}
    set_table_to(db_conn, "set", predecessor)
    with psycopg.connect(db_url(), autocommit=True) as conn:
        conn.execute(
            "CREATE FUNCTION syn_reject_history_delete() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'interrupted after history deletion'; END $$"
        )
        try:
            conn.execute(
                "CREATE TRIGGER syn_reject_history_delete AFTER DELETE ON schema_migrations "
                "FOR EACH STATEMENT EXECUTE FUNCTION syn_reject_history_delete()"
            )
            with pytest.raises(migrations.MigrationFailed, match="interrupted") as exc:
                migrations.apply_pending_migrations(
                    conn,
                    machine_name=machine_identity.name,
                    migrations_dir=tmp_path,
                    home=resolve_meiva_home(),
                )
            assert isinstance(exc.value.__cause__, psycopg.errors.RaiseException)
            assert migrations.applied_migration_names(conn) == predecessor
        finally:
            conn.execute("DROP TRIGGER IF EXISTS syn_reject_history_delete ON schema_migrations")
            conn.execute("DROP FUNCTION syn_reject_history_delete()")
        assert migrations.apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=tmp_path,
            home=resolve_meiva_home(),
        ) == [RESET_ANCHOR]
        migrations.check_schema_version(conn, migrations_dir=tmp_path)


def test_failed_reset_anchor_preserves_history_for_retry(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    machine_identity: MachineIdentityReader,
) -> None:
    from base.deploy.schema import migrations
    from base.deploy.schema.migration_history import PRE_RESET_SET, RESET_ANCHOR
    from base.deploy.schema.tests.migration_support import set_table_to

    anchor = tmp_path / f"{RESET_ANCHOR}.sql"
    anchor.write_text("SELECT 1 / 0;")
    init_repo(tmp_path)
    predecessor = {BASELINE_NAME, *PRE_RESET_SET}
    set_table_to(db_conn, "set", predecessor)
    with psycopg.connect(db_url(), autocommit=True) as conn:
        with pytest.raises(migrations.MigrationFailed, match="division by zero"):
            migrations.apply_pending_migrations(
                conn,
                machine_name=machine_identity.name,
                migrations_dir=tmp_path,
                home=resolve_meiva_home(),
            )
        assert migrations.applied_migration_names(conn) == predecessor
        anchor.write_text("SELECT 1;")
        assert migrations.apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=tmp_path,
            home=resolve_meiva_home(),
        ) == [RESET_ANCHOR]
        migrations.check_schema_version(conn, migrations_dir=tmp_path)
