# meiva:note
# A lock the test holds, so that concurrent requests meet at a chosen point of their work.
#
# Requests racing for one creation key are serialized by the birth transaction's advisory lock;
# requests that read a table are stopped by a lock on that table. A test that wants several
# requests to be inside the same stretch of their work at the same moment holds the lock itself,
# waits until the requests are queued on it, and releases it: the requests then resolve the race
# in the database exactly as clients arriving together would.

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from psycopg import sql

from base.agents.birth.creation_identity import lock_creation

__all__ = ["HeldLock", "held_creation_lock", "held_table_lock"]

_POLL_S = 0.02


class HeldLock:
    def __init__(self, db_url: str, holder: psycopg.Connection) -> None:
        self._db_url = db_url
        self.holder = holder

    # meiva:note
    # Block until `count` sessions are queued behind a lock, or fail the test.
    def wait_for_waiters(self, count: int, *, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        with psycopg.connect(self._db_url, autocommit=True) as watcher:
            while time.monotonic() < deadline:
                row = watcher.execute(
                    "SELECT count(DISTINCT pid) FROM pg_locks "
                    "WHERE NOT granted AND locktype IN ('advisory', 'relation')"
                ).fetchone()
                if row is not None and row[0] >= count:
                    return
                time.sleep(_POLL_S)
        raise AssertionError(f"fewer than {count} sessions queued behind the held lock")


# meiva:note
# Hold the birth lock of `key` until the block ends.
@contextmanager
def held_creation_lock(db_url: str, key: str) -> Generator[HeldLock]:
    with psycopg.connect(db_url) as holder:
        lock_creation(holder, key)
        try:
            yield HeldLock(db_url, holder)
        finally:
            holder.rollback()


# meiva:note
# Hold `table` against every reader and writer until the block ends.
#
# `HeldLock.holder` is the holding session: what it writes under the lock commits, and the
# lock is released, when the block ends normally.
@contextmanager
def held_table_lock(db_url: str, table: str) -> Generator[HeldLock]:
    with psycopg.connect(db_url) as holder:
        holder.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(table))
        )
        try:
            yield HeldLock(db_url, holder)
        except BaseException:
            holder.rollback()
            raise
        holder.commit()
