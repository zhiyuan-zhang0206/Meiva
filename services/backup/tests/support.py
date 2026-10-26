# meiva:note
# Shared scaffolding for the dump tests: a home that pins its own logical-backup
# passphrase, and a scratch database a test can fill and lock.
#
# Not a test module: pytest never collects this file.

from __future__ import annotations

import os
import random
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql

from services.backup.artifact import passphrase
from services.backup.layout import BackupTree


# meiva:note
# The backup tree of a home of this test's own, with its passphrase pinned the way a
# gateway birth pins it. Nothing is shared with the session home.
def minted_tree(home: Path) -> BackupTree:
    passphrase.ensure_pinned(home, passphrase=passphrase.mint(random.SystemRandom()))
    return BackupTree(home)


# meiva:note
# A database created next to the session database and dropped afterwards; yields its URL.
@contextmanager
def scratch_database(session_db_url: str) -> Generator[str]:
    base_url, _name = session_db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"meiva_test_backup_{os.getpid()}_{int(time.time() * 1_000_000)}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield f"{base_url}/{name}"
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


# meiva:note
# An open transaction holding an ACCESS EXCLUSIVE lock on one table of `url`'s database.
# A `pg_dump` of that database cannot take its share lock until the lock is released, so
# the dump blocks for real without any stand-in process.
@contextmanager
def held_table_lock(url: str) -> Generator[None]:
    with psycopg.connect(url) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS held_for_dump (n int)")
        conn.commit()
        conn.execute("LOCK TABLE held_for_dump IN ACCESS EXCLUSIVE MODE")
        try:
            yield
        finally:
            conn.rollback()
