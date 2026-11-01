# meiva:note
# Database operations for the append-only agent_harness_messages table used as
# the UI display store for external harnesses (claude_code, codex, antigravity,
# kimi_code). Native meiva continues using LangGraph checkpoints as both its
# execution truth and UI store.

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from psycopg import AsyncConnection, Connection

from base.agents.history.timeline_item import TimelineItem


def _int_from_row(row: tuple[object, ...] | None) -> int:
    if not row or row[0] is None:
        return 0
    val = row[0]
    return int(val) if isinstance(val, (int, str)) else 0


def get_next_harness_message_seq(conn: Connection[Any], agent_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return _int_from_row(cur.fetchone())


async def aget_next_harness_message_seq(conn: AsyncConnection[Any], agent_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return _int_from_row(await cur.fetchone())


def load_harness_message_count(conn: Connection[Any], agent_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return _int_from_row(cur.fetchone())


async def aload_harness_message_count(conn: AsyncConnection[Any], agent_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return _int_from_row(await cur.fetchone())


def load_harness_timeline_items(
    conn: Connection[Any],
    agent_id: int,
    *,
    limit: int | None = None,
    before_seq: int | None = None,
) -> list[TimelineItem]:
    query = "SELECT item FROM agent_harness_messages WHERE agent_id = %s"
    params: list[object] = [agent_id]
    if before_seq is not None:
        query += " AND seq < %s"
        params.append(before_seq)
    query += " ORDER BY seq ASC"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        return [TimelineItem.model_validate(row[0]) for row in rows]


async def aload_harness_timeline_items(
    conn: AsyncConnection[Any],
    agent_id: int,
    *,
    limit: int | None = None,
    before_seq: int | None = None,
) -> list[TimelineItem]:
    query = "SELECT item FROM agent_harness_messages WHERE agent_id = %s"
    params: list[object] = [agent_id]
    if before_seq is not None:
        query += " AND seq < %s"
        params.append(before_seq)
    query += " ORDER BY seq ASC"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)

    async with conn.cursor() as cur:
        await cur.execute(query, tuple(params))
        rows = await cur.fetchall()
        return [TimelineItem.model_validate(row[0]) for row in rows]


def append_harness_timeline_items(
    conn: Connection[Any],
    agent_id: int,
    harness: str,
    items: Sequence[TimelineItem],
    *,
    inbound_id: int | None = None,
) -> list[TimelineItem]:
    if not items:
        return []

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,))
        cur.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        start_seq = _int_from_row(cur.fetchone())

        persisted: list[TimelineItem] = []
        for i, item in enumerate(items):
            seq = start_seq + i
            item_id = f"{seq}.0"
            updated = item.model_copy(update={"item_id": item_id})
            persisted.append(updated)
            item_json = json.dumps(updated.model_dump(mode="json"))
            cur.execute(
                """
                INSERT INTO agent_harness_messages (agent_id, seq, harness, item, inbound_id, created_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, clock_timestamp()))
                """,
                (
                    agent_id,
                    seq,
                    harness,
                    item_json,
                    inbound_id or item.inbound_id,
                    item.created_at,
                ),
            )
        return persisted


async def aappend_harness_timeline_items(
    conn: AsyncConnection[Any],
    agent_id: int,
    harness: str,
    items: Sequence[TimelineItem],
    *,
    inbound_id: int | None = None,
) -> list[TimelineItem]:
    if not items:
        return []

    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,))
        await cur.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        start_seq = _int_from_row(await cur.fetchone())

        persisted: list[TimelineItem] = []
        for i, item in enumerate(items):
            seq = start_seq + i
            item_id = f"{seq}.0"
            updated = item.model_copy(update={"item_id": item_id})
            persisted.append(updated)
            item_json = json.dumps(updated.model_dump(mode="json"))
            await cur.execute(
                """
                INSERT INTO agent_harness_messages (agent_id, seq, harness, item, inbound_id, created_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, clock_timestamp()))
                """,
                (
                    agent_id,
                    seq,
                    harness,
                    item_json,
                    inbound_id or item.inbound_id,
                    item.created_at,
                ),
            )
        return persisted
