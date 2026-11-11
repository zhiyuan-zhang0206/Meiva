"""Isolated applied-set and Git fixtures for migration contracts."""

from __future__ import annotations

import os
import time
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql

from base.config import settings
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import (
    BASELINE_NAME,
    MIGRATION_LOCK_KEY,
    required_migration_set,
)

SCHEMA_SQL = Path(__file__).resolve().parents[4] / "db" / "schema.sql"
SYN = "29991231T235959_synthetic"
SYN2 = "29991231T235960_synthetic-two"
SYN_ORPHAN = "20260815T000001_synthetic-orphan"


def db_url() -> str:
    """The throwaway database the migration tests run against."""
    return settings.data_plane.db_url


def schema_sql_stamped_migration_names() -> list[str]:
    """The applied-set stamps db/schema.sql seeds, in file order.

    A folded strict migration keeps its stamp row so a fresh DB does not replay
    it. Reading the stamps from the file (rather than hard-coding a
    baseline+folded list) keeps this exact; the next folded migration cannot
    silently strand the fixture.
    """
    import re

    return re.findall(
        r"INSERT INTO schema_migrations \(name\) VALUES \('([^']+)'\)",
        SCHEMA_SQL.read_text(),
    )


@contextmanager
def throwaway_database(prefix: str) -> Generator[str, None, None]:
    base_url, _ = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"meiva_test_{prefix}_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def set_table_to(
    conn: psycopg.Connection, shape: str, rows: Iterable[str] | Iterable[int] | None = None
) -> None:
    """Rebuild schema_migrations in `shape` ('set' or 'legacy') with `rows`."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS schema_migrations")
        if shape == "set":
            cur.execute(
                "CREATE TABLE schema_migrations "
                "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            for name in rows or ():
                cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
        else:  # legacy
            cur.execute(
                "CREATE TABLE schema_migrations "
                "(version INT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            for v in rows or ():
                cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (v,))
    conn.commit()


@pytest.fixture(autouse=True)
def reset_schema_migrations_state(db_url: str) -> Iterator[None]:
    """Rebuild schema_migrations to the canonical baselined state before/after each
    test. Rebuilds the table itself (not just TRUNCATE) so a cutover test that
    swapped it to the legacy shape cannot leak into the next test or module.

    The per-test TRUNCATE behind `db_url` does not include schema_migrations (it is
    not business data), so this module self-manages it — the same pattern the
    pre-cutover suite used. It requests the database URL, not a connection: an
    open `db_conn` transaction that read the table would block this teardown's
    DROP TABLE.
    """

    def _reseed(names: list[str]) -> None:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            set_table_to(conn, "set", names)

    _canonical_applied = sorted(required_migration_set(migrations_dir=MIGRATIONS_DIR))
    _reseed([BASELINE_NAME])
    yield
    # Restore the canonical fully-applied state (baseline + every migration file),
    # NOT a hard-coded prefix list: a freshly folded strict migration must stay
    # in the canonical applied set, or a later cmd_migrations_apply in the same
    # worker re-runs it against an already-current schema and fails loudly
    # (DuplicateColumn — the class that hit PR #1587's migration).
    _reseed(_canonical_applied)


def git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603 — test git fixture, fixed argv


# meiva:note
# Make `repo` a git worktree with an initial commit. The loader's
# git-tracking gate (#998) applies only what git tracks, so any test that
# supplies a migrations directory must model a real checkout. Call AFTER writing
# the files that should be tracked (they land in the initial commit); files
# written afterwards are untracked by construction.
def init_repo(repo: Path) -> None:
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")


def try_lock_from_other_conn() -> bool:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        row = cur.fetchone()
        assert row is not None
        got = row[0]
        if got:
            cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
        return bool(got)


LEGACY_NOTICE_COLUMNS = (
    "local_id",
    "priority",
    "require_response",
    "blocking",
    "updated_at",
    "task_id",
    "expire_at",
)


# meiva:note
# The baseline no longer carries these columns (the contract migration drops them), so a test of an
# older migration over agent_notices rebuilds them, as the schema that migration upgrades had them,
# before it inserts rows that use them.
def restore_legacy_notice_columns(conn: psycopg.Connection) -> None:
    conn.execute(
        "ALTER TABLE agent_notices "
        "ADD COLUMN local_id INTEGER, "
        "ADD COLUMN priority TEXT NOT NULL DEFAULT 'P2', "
        "ADD COLUMN require_response BOOLEAN NOT NULL DEFAULT TRUE, "
        "ADD COLUMN blocking BOOLEAN NOT NULL DEFAULT FALSE, "
        "ADD COLUMN updated_at TIMESTAMPTZ, "
        "ADD COLUMN task_id BIGINT, "
        "ADD COLUMN expire_at TIMESTAMPTZ"
    )


# meiva:note
# The question, publication and receipt tables carry content names since the split migration; a test
# of an older migration upgrades the schema that had the old names, so it puts them back first (the
# exact inverse of that migration's renames and constraint tightening) before it builds its rows.
RESTORE_PRE_SPLIT_NAMES = """
ALTER TABLE agent_questions DROP CONSTRAINT agent_questions_resolution_legal;
ALTER TABLE agent_questions DROP CONSTRAINT agent_questions_question_is_object;
ALTER TABLE agent_questions DROP CONSTRAINT agent_questions_answer_is_answered;
ALTER TABLE agent_questions ALTER COLUMN question DROP NOT NULL;
ALTER TABLE agent_questions RENAME TO agent_notices;
ALTER SEQUENCE agent_questions_id_seq RENAME TO agent_notices_id_seq;
ALTER INDEX agent_questions_pkey RENAME TO agent_notices_pkey;
ALTER INDEX agent_questions_open_idx RENAME TO agent_notices_open_idx;
ALTER TABLE agent_notices RENAME CONSTRAINT agent_questions_agent_id_fkey TO agent_notices_agent_id_fkey;
ALTER TABLE agent_notices RENAME CONSTRAINT agent_questions_resolution_pair TO agent_notices_resolution_pair;
ALTER TABLE agent_notices RENAME CONSTRAINT agent_questions_answered_has_reply TO agent_notices_answered_has_reply;
ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_resolution_check
    CHECK (resolution IN ('answered', 'dismissed', 'read', 'withdrawn', 'superseded', 'expired'));
ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_question_is_object
    CHECK (question IS NULL OR jsonb_typeof(question) = 'object');
ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_answer_requires_question
    CHECK (answer IS NULL OR (jsonb_typeof(answer) = 'object' AND question IS NOT NULL
                              AND resolution IS NOT DISTINCT FROM 'answered'));
ALTER TABLE agent_publications RENAME TO agent_user_messages;
ALTER SEQUENCE agent_publications_id_seq RENAME TO agent_user_messages_id_seq;
ALTER INDEX agent_publications_pkey RENAME TO agent_user_messages_pkey;
ALTER INDEX agent_publications_by_agent RENAME TO agent_user_messages_by_agent;
ALTER INDEX agent_publications_agent_id_source_key_key RENAME TO agent_user_messages_agent_id_source_key_key;
ALTER TABLE agent_user_messages RENAME CONSTRAINT agent_publications_agent_id_fkey TO agent_user_messages_agent_id_fkey;
ALTER TABLE agent_user_messages RENAME CONSTRAINT agent_publications_mode_check TO agent_user_messages_mode_check;
ALTER TABLE agent_user_messages RENAME CONSTRAINT agent_publications_suggestions_check TO agent_user_messages_suggestions_check;
ALTER TABLE agent_user_messages RENAME CONSTRAINT agent_publications_check TO agent_user_messages_check;
ALTER TABLE agent_user_messages RENAME CONSTRAINT agent_publications_content_check TO agent_user_messages_content_check;
ALTER TABLE agent_publication_seen RENAME TO agent_user_seen;
ALTER INDEX agent_publication_seen_pkey RENAME TO agent_user_seen_pkey;
ALTER TABLE agent_user_seen RENAME CONSTRAINT agent_publication_seen_agent_id_fkey TO agent_user_seen_agent_id_fkey;
ALTER TABLE agent_user_seen RENAME CONSTRAINT agent_publication_seen_seen_id_check TO agent_user_seen_seen_id_check;
COMMENT ON TABLE agent_user_seen IS 'The user has seen deliveries (agent_user_messages.id) up to seen_id; no row means none.';
ALTER TABLE keyed_operation_receipts RENAME TO notice_operation_receipts;
ALTER INDEX keyed_operation_receipts_pkey RENAME TO notice_operation_receipts_pkey;
ALTER TABLE notice_operation_receipts RENAME CONSTRAINT keyed_operation_receipts_operation_key_check TO notice_operation_receipts_operation_key_check;
"""


def restore_pre_split_names(conn: psycopg.Connection) -> None:
    conn.execute(cast(LiteralString, RESTORE_PRE_SPLIT_NAMES))
