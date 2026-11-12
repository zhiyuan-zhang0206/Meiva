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
from psycopg import AsyncConnection, Connection, Cursor

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
INSERT INTO agent_harness_messages (agent_id, seq, harness, item, inbound_id, created_at, call_id)
VALUES (%s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, clock_timestamp()), %s)
"""


# meiva:note
# The value of the `call_id` column of a timeline item: the CLI's id of the tool call an `agent_code` item
# recorded, None for every other item. Every insert of an item writes it, so the column and the item agree;
# the column exists so that the lookup by call id has an index that an in-place revision of `item` does not
# touch (an index on an expression of `item` would stop those updates from being heap-only).
# meiva:link base/lm/harness/docs/subagent-text.md
def call_id_of(item: TimelineItem) -> str | None:
    return item.tool_use_id if item.kind == "agent_code" and item.tool_use_id else None


def _int_from_row(row: tuple[object, ...] | None) -> int:
    if not row or row[0] is None:
        return 0
    val = row[0]
    return int(val) if isinstance(val, (int, str)) else 0


# meiva:note
# Copy one agent's display timeline as another's first rows; returns the row count. The sequence
# numbers and revisions carry over unchanged, so item ids stay stable and the new agent's own rows
# continue after them. `inbound_id` is cleared: it names a row of the source's inbound queue, which
# the new agent does not own. The delivery records of what the source sent to the user
# (`agent_publications`) are deliberately not copied, so the copy raises no unread for the new
# agent.
# meiva:link base/lm/harness/docs/fork.md
def copy_harness_timeline(
    cur: Cursor[tuple[object, ...]], source_agent_id: int, new_agent_id: int
) -> int:
    cur.execute(
        "INSERT INTO agent_harness_messages "
        "(agent_id, seq, harness, item, inbound_id, created_at, source_key, projection_version, call_id) "
        "SELECT %s, seq, harness, item, NULL, created_at, source_key, projection_version, call_id "
        "FROM agent_harness_messages "
        "WHERE agent_id = %s",
        (new_agent_id, source_agent_id),
    )
    return cur.rowcount


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


# meiva:note
# The statement of `aget_harness_call_item_ids` (parameters: the agent id, the list of call ids), public so that a
# test can ask the planner which index answers it.
HARNESS_CALL_ITEMS_SQL = (
    "SELECT call_id, MAX(seq) FROM agent_harness_messages WHERE agent_id = %s "
    "AND call_id IS NOT NULL AND call_id = ANY(%s) GROUP BY 1"
)


# meiva:note
# The timeline item ids of the `agent_code` items that recorded the CLI tool calls `tool_use_ids`: each id that has
# one, by its call id (a call id with no item is absent from the answer). A sub-agent can keep working after the
# graph invocation, or the process, that saw its spawning call ended, so the call is found by the CLI's own id,
# which the item carries in the `call_id` column. One statement for all the ids, answered by the partial index
# `idx_agent_harness_messages_call` (agent, call id): the cost is that of the ids asked for,
# not of the rows the agent has. The newest item wins when a call id was recorded twice.
# meiva:link base/lm/harness/docs/subagent-text.md
async def aget_harness_call_item_ids(
    conn: AsyncConnection, agent_id: int, tool_use_ids: Sequence[str]
) -> dict[str, str]:
    if not tool_use_ids:
        return {}
    async with conn.cursor() as cur:
        await cur.execute(HARNESS_CALL_ITEMS_SQL, (agent_id, list(tool_use_ids)))
        found: list[tuple[object, ...]] = await cur.fetchall()
    return {str(row[0]): f"h{row[1]}.0" for row in found}


# meiva:note
# The timeline item id of the `agent_code` item that recorded the CLI tool call `tool_use_id`, or None: one call
# id of `aget_harness_call_item_ids`.
# meiva:link base/lm/harness/docs/subagent-text.md
async def aget_harness_call_item_id(
    conn: AsyncConnection, agent_id: int, tool_use_id: str
) -> str | None:
    return (await aget_harness_call_item_ids(conn, agent_id, [tool_use_id])).get(tool_use_id)


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
                    call_id_of(updated),
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
                    call_id_of(updated),
                ),
            )
        return persisted


_INSERT_KEYED_HARNESS_TIMELINE_ITEM = """
INSERT INTO agent_harness_messages
    (agent_id, seq, harness, item, inbound_id, created_at, source_key, projection_version, call_id)
VALUES (%s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, clock_timestamp()), %s, %s, %s)
ON CONFLICT (agent_id, source_key) WHERE source_key IS NOT NULL DO NOTHING
RETURNING seq
"""


# meiva:note
# Append the items projected from transcript entries, each under its stable `source_key`: an item whose key
# the agent already has is skipped and gives no row and no sequence number, so the same entries read twice
# write once and the sequence stays contiguous. The timeline stays append-only: the item gets the next `seq`
# in the order the items are given. The caller holds the transaction and has taken its owner lock
# (`lock_inbound_owner`); the agent row lock here only serializes the sequence. Returns the items that were
# written, with their ids.
# meiva:link base/agents/history/docs/timeline/timeline.md
async def aappend_keyed_timeline_items(
    conn: AsyncConnection,
    agent_id: int,
    harness: str,
    keyed: Sequence[tuple[str, TimelineItem]],
    *,
    projection_version: int,
) -> list[TimelineItem]:
    if not keyed:
        return []
    persisted: list[TimelineItem] = []
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,))
        await cur.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        next_seq = _int_from_row(await cur.fetchone())
        for source_key, item in keyed:
            updated = item.model_copy(update={"item_id": f"h{next_seq}.0"})
            await cur.execute(
                _INSERT_KEYED_HARNESS_TIMELINE_ITEM,
                (
                    agent_id,
                    next_seq,
                    harness,
                    json.dumps(updated.model_dump(mode="json")),
                    item.inbound_id,
                    item.created_at,
                    source_key,
                    projection_version,
                    call_id_of(updated),
                ),
            )
            if await cur.fetchone() is not None:
                persisted.append(updated)
                next_seq += 1
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
# meiva:link base/agents/history/docs/timeline/timeline.md
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
        (
            agent_id,
            seq,
            harness,
            item.model_dump_json(),
            item.inbound_id,
            item.created_at,
            call_id_of(item),
        ),
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


# meiva:note
# Whether the agent's timeline has any item yet: how the writer tells an agent with a history (one that was on
# the event path, a fork that inherited its parent's) from a fresh one when it starts on a session.
async def ahas_harness_timeline(conn: AsyncConnection, agent_id: int) -> bool:
    cursor = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM agent_harness_messages WHERE agent_id = %s)", (agent_id,)
    )
    row = await cursor.fetchone()
    return bool(row and row[0])
