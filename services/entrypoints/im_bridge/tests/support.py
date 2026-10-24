from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import LiteralString

import psycopg
from psycopg import sql

from services.entrypoints.im_bridge.types import (
    IMAdapter,
    OutboundAdapterKind,
    OutboundChunk,
    PreparedOutboundSend,
)

__all__ = [
    "DurableAdapter",
    "failing_insert",
    "failing_update",
    "held_table_lock",
    "owned_tasks",
]


# meiva:note
# A task group the test owns: it ends by cancelling whatever the test left running.
@asynccontextmanager
async def owned_tasks() -> AsyncGenerator[asyncio.TaskGroup]:
    with contextlib.suppress(asyncio.CancelledError):
        async with asyncio.TaskGroup() as tasks:
            yield tasks
            raise asyncio.CancelledError


# meiva:note
# An adapter double with the durable timeline capability the core requires of every channel: a
# fixed non-secret account, a faithful credential-free rendering, and delivery through its own
# `send`. Subclasses script `send` and the channel; the real adapters define these three themselves.
class DurableAdapter(IMAdapter):
    async def outbound_account_id(self) -> str:
        return "test-account"

    async def prepare_timeline(self, text: str) -> PreparedOutboundSend:
        return PreparedOutboundSend(
            adapter_kind=OutboundAdapterKind(self.channel + "-v1"),
            account_id="test-account",
            chunks=(OutboundChunk(text=text),),
            markdown=True,
        )

    async def send_prepared_outbound(self, chat_id: str, prepared: PreparedOutboundSend) -> None:
        for chunk in prepared.chunks:
            await self.send(chat_id, chunk.text, markdown=prepared.markdown)


# meiva:note
# Every INSERT into `table` whose new row satisfies `when` (a SQL condition over `NEW`) raises
# `message` after it wrote its row, until exit. The throwaway Postgres itself fails the
# statement and rolls the transaction back, as a database error would.
@contextmanager
def failing_insert(
    conn: psycopg.Connection,
    table: str,
    *,
    when: LiteralString = "true",
    message: str = "injected insert fault",
) -> Generator[None]:
    function = sql.Identifier(f"fault_raise_{table}")
    trigger = sql.Identifier(f"fault_{table}")
    target = sql.Identifier(table)
    conn.commit()
    conn.execute(
        sql.SQL(
            "CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS "
            "$body$ BEGIN RAISE EXCEPTION {message}; END $body$"
        ).format(function=function, message=sql.Literal(message))
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER {trigger} AFTER INSERT ON {target} "
            "FOR EACH ROW WHEN ({when}) EXECUTE FUNCTION {function}()"
        ).format(trigger=trigger, target=target, function=function, when=sql.SQL(when))
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(
            sql.SQL("DROP TRIGGER {trigger} ON {target}").format(trigger=trigger, target=target)
        )
        conn.execute(sql.SQL("DROP FUNCTION {function}()").format(function=function))
        conn.commit()


# meiva:note
# Every UPDATE of `table` whose row satisfies `when` (a SQL condition over `NEW` and `OLD`)
# raises `message`, until exit; the server aborts the statement and rolls the transaction back.
@contextmanager
def failing_update(
    conn: psycopg.Connection,
    table: str,
    *,
    when: LiteralString = "true",
    message: str = "injected update fault",
) -> Generator[None]:
    function = sql.Identifier(f"fault_raise_update_{table}")
    trigger = sql.Identifier(f"fault_update_{table}")
    target = sql.Identifier(table)
    conn.commit()
    conn.execute(
        sql.SQL(
            "CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS "
            "$body$ BEGIN IF {when} THEN RAISE EXCEPTION {message}; END IF; RETURN NEW; END $body$"
        ).format(function=function, when=sql.SQL(when), message=sql.Literal(message))
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER {trigger} BEFORE UPDATE ON {target} "
            "FOR EACH ROW EXECUTE FUNCTION {function}()"
        ).format(trigger=trigger, target=target, function=function)
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute(
            sql.SQL("DROP TRIGGER {trigger} ON {target}").format(trigger=trigger, target=target)
        )
        conn.execute(sql.SQL("DROP FUNCTION {function}()").format(function=function))
        conn.commit()


# meiva:note
# A table lock the test holds, so that work which reads the table queues behind it: block
# until `count` sessions wait on locks, or fail the test.
class HeldLock:
    def __init__(self, db_url: str) -> None:
        self._db_url = db_url

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
                time.sleep(0.02)
        raise AssertionError(f"fewer than {count} sessions queued behind the held lock")


# meiva:note
# Hold `table` in lock `mode` (by default against every reader and writer) until the block ends.
@contextmanager
def held_table_lock(
    db_url: str, table: str, *, mode: LiteralString = "ACCESS EXCLUSIVE"
) -> Generator[HeldLock]:
    with psycopg.connect(db_url) as holder:
        holder.execute(
            sql.SQL("LOCK TABLE {} IN {} MODE").format(sql.Identifier(table), sql.SQL(mode))
        )
        try:
            yield HeldLock(db_url)
        except BaseException:
            holder.rollback()
            raise
        holder.commit()
