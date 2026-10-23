# meiva:note
# A checkpoint store the gateway cannot read, made on the real database.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import psycopg

__all__ = ["checkpoint_store_unreadable"]


# meiva:note
# Take the `checkpoints` table away for the block: every read of it fails like a store outage.
@contextmanager
def checkpoint_store_unreadable(db_url: str) -> Generator[None]:
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE checkpoints RENAME TO checkpoints_unreadable")
        try:
            yield
        finally:
            conn.execute("ALTER TABLE checkpoints_unreadable RENAME TO checkpoints")
