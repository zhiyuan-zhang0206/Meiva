from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg

from base.db.transaction import ConnectionSource


# meiva:note
# A pool whose `lose_on`-th lent connection commits for real, then reports a failure to the caller.
#
# That is the one fault a database cannot be asked for: the work is durable and the answer is lost,
# so the caller must find it again on retry. Every other lending runs untouched; a body that
# raises rolls back and its error reaches the caller instead of the lost acknowledgement.
class LostAckPool:
    def __init__(self, pool: ConnectionSource, *, lose_on: int, message: str) -> None:
        if lose_on < 1:
            raise ValueError("lose_on counts lendings from 1")
        self._pool = pool
        self._lose_on = lose_on
        self._message = message
        self.lent = 0

    @contextmanager
    def connection(self, timeout: float | None = None) -> Generator[psycopg.Connection[Any]]:
        self.lent += 1
        this = self.lent
        with self._pool.connection(timeout=timeout) as conn:
            yield conn
        if this == self._lose_on:
            raise psycopg.OperationalError(self._message)
