# meiva:note
# Database operations for the agent_harness_messages table used as
# the UI display store for external harnesses (claude_code, codex, antigravity).
# Native meiva continues using LangGraph checkpoints as both its
# execution truth and UI store.

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from psycopg import AsyncConnection, Connection

from base.agents.history.closing_request import FullHistory, single_segment_history
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.kwargs import MeivaMsgType, NoteTag
from base.db.rows import aquery_all, aquery_one, query_all
from base.native_process.runtime_incarnation import RuntimeIncarnation, StaleIncarnationError


# meiva:note
# The `item` column of an `agent_harness_messages` row, a jsonb timeline item still to validate.
@dataclass(frozen=True)
class _ItemRow:
    item: object


_INSERT_HARNESS_TIMELINE_ITEM = """
INSERT INTO agent_harness_messages (agent_id, seq, harness, item, inbound_id, created_at)
VALUES (%s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, clock_timestamp()))
"""


def _int_from_row(row: tuple[object, ...] | None) -> int:
    if not row or row[0] is None:
        return 0
    val = row[0]
    return int(val) if isinstance(val, (int, str)) else 0


def get_next_harness_message_seq(conn: Connection, agent_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return _int_from_row(cur.fetchone())


async def aget_next_harness_message_seq(conn: AsyncConnection, agent_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return _int_from_row(await cur.fetchone())


def load_harness_message_count(conn: Connection, agent_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return _int_from_row(cur.fetchone())


def has_harness_messages(conn: Connection, agent_id: int, *, since: str | None = None) -> bool:
    query = "SELECT 1 FROM agent_harness_messages WHERE agent_id = %s"
    params: list[object] = [agent_id]
    if since is not None:
        query += " AND created_at >= %s"
        params.append(since)
    query += " LIMIT 1"
    with conn.cursor() as cur:
        cur.execute(query, tuple(params))
        return cur.fetchone() is not None


async def aload_harness_message_count(conn: AsyncConnection, agent_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        return _int_from_row(await cur.fetchone())


def _normalize_harness_item(item: TimelineItem) -> TimelineItem:
    if not item.item_id.startswith("h"):
        return item.model_copy(update={"item_id": f"h{item.item_id}"})
    return item


def load_harness_timeline_items(
    conn: Connection,
    agent_id: int,
    *,
    limit: int | None = None,
    before_seq: int | None = None,
    since: str | None = None,
) -> list[TimelineItem]:
    query = "SELECT item FROM agent_harness_messages WHERE agent_id = %s"
    params: list[object] = [agent_id]
    if before_seq is not None:
        query += " AND seq < %s"
        params.append(before_seq)
    if since is not None:
        query += " AND created_at >= %s"
        params.append(since)
    query += " ORDER BY seq ASC"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)

    rows = query_all(conn, _ItemRow, query, tuple(params))
    return [_normalize_harness_item(TimelineItem.model_validate(row.item)) for row in rows]


async def aload_harness_timeline_items(
    conn: AsyncConnection,
    agent_id: int,
    *,
    limit: int | None = None,
    before_seq: int | None = None,
    since: str | None = None,
) -> list[TimelineItem]:
    query = "SELECT item FROM agent_harness_messages WHERE agent_id = %s"
    params: list[object] = [agent_id]
    if before_seq is not None:
        query += " AND seq < %s"
        params.append(before_seq)
    if since is not None:
        query += " AND created_at >= %s"
        params.append(since)
    query += " ORDER BY seq ASC"
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)

    rows = await aquery_all(conn, _ItemRow, query, tuple(params))
    return [_normalize_harness_item(TimelineItem.model_validate(row.item)) for row in rows]


def append_harness_timeline_items(
    conn: Connection,
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
            item_id = f"h{seq}.0"
            updated = item.model_copy(update={"item_id": item_id})
            persisted.append(updated)
            item_json = json.dumps(updated.model_dump(mode="json"))
            cur.execute(
                _INSERT_HARNESS_TIMELINE_ITEM,
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
    conn: AsyncConnection,
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
            item_id = f"h{seq}.0"
            updated = item.model_copy(update={"item_id": item_id})
            persisted.append(updated)
            item_json = json.dumps(updated.model_dump(mode="json"))
            await cur.execute(
                _INSERT_HARNESS_TIMELINE_ITEM,
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


@dataclass(frozen=True)
class _HarnessTimelineOwner:
    status: str
    runtime_generation: UUID | None
    runtime_owner: UUID | None


@dataclass(frozen=True)
class _HarnessTimelineRow:
    harness: str
    item: str


# meiva:note
# Insert the next flat activity or advance the revision of an existing one. None denotes
# a sequence, identity, or revision conflict; an obsolete runtime raises before reading
# or writing timeline items.
# meiva:link base/agents/history/docs/timeline/timeline.meiva.okf.md
async def aupsert_harness_timeline_item(
    conn: AsyncConnection,
    agent_id: int,
    harness: str,
    item: TimelineItem,
    *,
    incarnation: RuntimeIncarnation | None = None,
) -> TimelineItem | None:
    item = _normalize_harness_item(item)
    seq = _activity_seq(item.item_id)
    revision = item.activity_revision
    if revision is None:
        raise ValueError("harness activity requires a revision")
    async with conn.transaction():
        if not await _lock_activity_owner(conn, agent_id, incarnation):
            return None
        row = await aquery_one(
            conn,
            _HarnessTimelineRow,
            "SELECT harness, item::text FROM agent_harness_messages WHERE agent_id = %s AND seq = %s",
            (agent_id, seq),
        )
        if row is None:
            return await _insert_harness_activity(conn, agent_id, seq, harness, item)
        if row.harness != harness:
            return None
        previous = _normalize_harness_item(TimelineItem.model_validate_json(row.item))
        if not _can_advance_activity(previous, item, revision):
            return item if previous == item else None
        await conn.execute(
            "UPDATE agent_harness_messages SET item = %s::jsonb WHERE agent_id = %s AND seq = %s",
            (item.model_dump_json(), agent_id, seq),
        )
        return item


def _activity_seq(item_id: str) -> int:
    message, separator, block = item_id.partition(".")
    message = message.removeprefix("h")
    if not separator or block != "0" or not message.isascii() or not message.isdecimal():
        raise ValueError("harness activity requires a flat timeline item id")
    if len(message) > 10:
        raise ValueError("harness activity requires a canonical timeline sequence")
    seq = int(message)
    if message != str(seq) or seq > 2_147_483_647:
        raise ValueError("harness activity requires a canonical timeline sequence")
    return seq


async def _lock_activity_owner(
    conn: AsyncConnection, agent_id: int, incarnation: RuntimeIncarnation | None
) -> bool:
    if incarnation is not None:
        incarnation.require_agent(agent_id)
    owner = await aquery_one(
        conn,
        _HarnessTimelineOwner,
        "SELECT status, runtime_generation, runtime_owner FROM agents_meta WHERE id = %s FOR UPDATE",
        (agent_id,),
    )
    if incarnation is not None and (
        owner is None
        or owner.status == "terminated"
        or (owner.runtime_generation, owner.runtime_owner)
        != (incarnation.generation, incarnation.owner)
    ):
        raise StaleIncarnationError(f"stale harness activity write for agent {agent_id}")
    return owner is not None


def _can_advance_activity(previous: TimelineItem, item: TimelineItem, revision: int) -> bool:
    return (
        previous.activity_revision is not None
        and previous.activity_revision < revision
        and previous.item_id == item.item_id
        and previous.kind == item.kind
        and previous.inbound_id == item.inbound_id
    )


async def _insert_harness_activity(
    conn: AsyncConnection, agent_id: int, seq: int, harness: str, item: TimelineItem
) -> TimelineItem | None:
    if seq != await aget_next_harness_message_seq(conn, agent_id):
        return None
    item = _normalize_harness_item(item)
    await conn.execute(
        _INSERT_HARNESS_TIMELINE_ITEM,
        (agent_id, seq, harness, item.model_dump_json(), item.inbound_id, item.created_at),
    )
    return item


# meiva:note
# Map one external harness timeline item to its normalized LangChain message representation.
def harness_item_to_message(item: TimelineItem) -> BaseMessage:
    ts = item.created_at
    if item.kind == "inbound_chat":
        kwargs: dict[str, object] = {
            "meiva_msg_type": MeivaMsgType.INBOUND,
            "meiva_created_at": ts,
            "meiva_picked_up_at": ts,
        }
        if item.inbound_id is not None:
            kwargs["meiva_inbound_id"] = item.inbound_id
        return HumanMessage(
            content=item.payload,
            id=item.item_id,
            additional_kwargs=kwargs,
        )
    if item.kind in ("system_marker", "attach"):
        kwargs = {
            "meiva_msg_type": MeivaMsgType.SYSTEM_NOTE,
            "meiva_note_tag": NoteTag.TASK,
            "meiva_created_at": ts,
            "meiva_picked_up_at": ts,
        }
        if item.inbound_id is not None:
            kwargs["meiva_inbound_id"] = item.inbound_id
        return SystemMessage(
            content=item.payload,
            id=item.item_id,
            additional_kwargs=kwargs,
        )
    if item.kind == "agent_reasoning":
        return AIMessage(
            content=[{"type": "thinking", "thinking": item.payload}],
            id=item.item_id,
            additional_kwargs={
                "meiva_created_at": ts,
                "meiva_picked_up_at": ts,
                "meiva_reasoning_ms_by_block": {0: item.reasoning_ms or 0},
            },
        )
    if item.kind == "agent_code":
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": item.source or "execute_code",
                    "args": {"code": item.payload},
                    "id": item.item_id,
                }
            ],
            id=item.item_id,
            additional_kwargs={
                "meiva_created_at": ts,
                "meiva_picked_up_at": ts,
                "meiva_code_ms_by_block": {0: item.code_elapsed_ms or 0},
            },
        )
    if item.kind == "code_output":
        return ToolMessage(
            content=item.payload,
            tool_call_id=item.item_id,
            id=item.item_id,
            additional_kwargs={
                "meiva_msg_type": MeivaMsgType.EXEC_OUTPUT,
                "meiva_created_at": ts,
                "meiva_picked_up_at": ts,
                "meiva_exec_ms": item.exec_ms or 0,
                "meiva_exec_body_start": 0,
            },
        )
    if item.kind == "agent_chat":
        return AIMessage(
            content=item.payload,
            id=item.item_id,
            additional_kwargs={
                "meiva_created_at": ts,
                "meiva_picked_up_at": ts,
            },
        )
    return SystemMessage(
        content=item.payload,
        id=item.item_id,
        additional_kwargs={
            "meiva_created_at": ts,
            "meiva_picked_up_at": ts,
        },
    )


# meiva:note
# Convert external harness timeline items into an uncompacted FullHistory segment.
def harness_timeline_to_full_history(items: Sequence[TimelineItem]) -> FullHistory:
    messages = [harness_item_to_message(item) for item in items]
    return single_segment_history(messages)
