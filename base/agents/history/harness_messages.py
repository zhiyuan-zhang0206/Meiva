# meiva:note
# Database operations for the agent_harness_messages table used as
# the UI display store for external harnesses (claude_code, codex, antigravity).
# Native meiva continues using LangGraph checkpoints as both its
# execution truth and UI store.

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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
INSERT INTO agent_harness_messages (agent_id, seq, harness, item, inbound_id, created_at)
VALUES (%s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, clock_timestamp()))
"""


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
        "(agent_id, seq, harness, item, inbound_id, created_at, source_key, projection_version) "
        "SELECT %s, seq, harness, item, NULL, created_at, source_key, projection_version "
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
# The timeline item id of the `agent_code` item that recorded the CLI tool call `tool_use_id`, or None.
# A sub-agent can keep working after the graph invocation that saw its spawning call ended, and the
# next invocation's projector has no memory of that call; the item itself carries the CLI's id, so
# the call is found by it. The filter reads one agent's rows, which is what an agent's
# timeline is, and is asked once per call id per invocation.
# meiva:link base/lm/harness/docs/subagent-text.md
async def aget_harness_call_item_id(
    conn: AsyncConnection, agent_id: int, tool_use_id: str
) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT seq FROM agent_harness_messages WHERE agent_id = %s "
            "AND item->>'kind' = 'agent_code' AND item->>'tool_use_id' = %s "
            "ORDER BY seq DESC LIMIT 1",
            (agent_id, tool_use_id),
        )
        row = await cur.fetchone()
    return None if row is None else f"h{row[0]}.0"


# meiva:note
# The `agent_chat` items among an agent's latest `rows` timeline rows, oldest first. A bounded read of the tail
# (`rows` is the bound, whatever the length of the timeline), for the replies a process may have committed and
# not yet handed to delivery, which the idempotent capture then offers again.
async def aload_recent_agent_replies(
    conn: AsyncConnection, agent_id: int, *, rows: int
) -> list[TimelineItem]:
    found = await aquery_all(
        conn,
        _ItemRow,
        "SELECT item FROM (SELECT seq, item FROM agent_harness_messages WHERE agent_id = %s "
        "ORDER BY seq DESC LIMIT %s) AS tail WHERE item->>'kind' = 'agent_chat' ORDER BY seq",
        (agent_id, rows),
    )
    return [TimelineItem.model_validate(row.item) for row in found]


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


_INSERT_KEYED_HARNESS_TIMELINE_ITEM = """
INSERT INTO agent_harness_messages
    (agent_id, seq, harness, item, inbound_id, created_at, source_key, projection_version)
VALUES (%s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, clock_timestamp()), %s, %s)
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
                ),
            )
            if await cur.fetchone() is not None:
                persisted.append(updated)
                next_seq += 1
    return persisted


# meiva:note
# Where one transcript record has been read to: the adapter's opaque `position`, and the `rebuild_ordinal`,
# the ordinal of the last stored row at a quiet point (nothing open that a restart would need to see), from
# which the projector's state is rebuilt out of the stored rows. Both are written in the transaction that
# writes the rows they cover; the row is one small row per record whatever the length of the conversation.
@dataclass(frozen=True)
class StoredPosition:
    record: str
    position: str
    rebuild_ordinal: int


async def aload_transcript_positions(
    conn: AsyncConnection, agent_id: int, session_id: str
) -> dict[str, StoredPosition]:
    rows = await aquery_all(
        conn,
        StoredPosition,
        "SELECT record, position, rebuild_ordinal FROM agent_harness_transcript_positions "
        "WHERE agent_id = %s AND session_id = %s",
        (agent_id, session_id),
    )
    return {row.record: row for row in rows}


async def aset_transcript_position(
    conn: AsyncConnection,
    agent_id: int,
    session_id: str,
    stored: StoredPosition,
    cli_version: str | None,
) -> None:
    await conn.execute(
        "INSERT INTO agent_harness_transcript_positions "
        "(agent_id, session_id, record, position, rebuild_ordinal, cli_version) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (agent_id, session_id, record) DO UPDATE SET position = EXCLUDED.position, "
        "rebuild_ordinal = EXCLUDED.rebuild_ordinal, "
        "cli_version = COALESCE(EXCLUDED.cli_version, "
        "agent_harness_transcript_positions.cli_version), updated_at = clock_timestamp()",
        (agent_id, session_id, stored.record, stored.position, stored.rebuild_ordinal, cli_version),
    )


# meiva:note
# The whitelisted rows of a transcript record, appended once and never updated: a row the agent already has
# (same session, record and ordinal) is skipped, so a read that is repeated writes nothing twice. Only the
# rows of the read are written, so what one call writes does not grow with what is already stored.
# meiva:link future/agent/transcript-timeline-source.md
async def aappend_transcript_rows(
    conn: AsyncConnection,
    agent_id: int,
    session_id: str,
    rows: Sequence[tuple[str, int, Mapping[str, object]]],
) -> int:
    if not rows:
        return 0
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO agent_harness_transcript_rows (agent_id, session_id, record, ordinal, row) "
            "VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
            [
                (agent_id, session_id, record, ordinal, json.dumps(row))
                for record, ordinal, row in rows
            ],
        )
        return cur.rowcount


@dataclass(frozen=True)
class _StoredRowRow:
    ordinal: int
    row: object


# meiva:note
# The stored rows of one record after an ordinal, in order, at most `limit` of them: how a restart rebuilds
# the projector's state (from the last rebuild point on, never the whole session) and how a re-projection
# reads a session in bounded batches.
async def aload_transcript_rows(
    conn: AsyncConnection,
    agent_id: int,
    session_id: str,
    record: str,
    *,
    after_ordinal: int,
    limit: int,
) -> list[tuple[int, object]]:
    rows = await aquery_all(
        conn,
        _StoredRowRow,
        "SELECT ordinal, row FROM agent_harness_transcript_rows "
        "WHERE agent_id = %s AND session_id = %s AND record = %s AND ordinal > %s "
        "ORDER BY ordinal LIMIT %s",
        (agent_id, session_id, record, after_ordinal, limit),
    )
    return [(row.ordinal, row.row) for row in rows]


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


# meiva:note
# Whether the agent's timeline has any item yet: how the writer tells an agent with a history (one that was on
# the event path, a fork that inherited its parent's) from a fresh one when it starts on a session.
async def ahas_harness_timeline(conn: AsyncConnection, agent_id: int) -> bool:
    cursor = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM agent_harness_messages WHERE agent_id = %s)", (agent_id,)
    )
    row = await cursor.fetchone()
    return bool(row and row[0])


# meiva:note
# The ordinal below which a rebuild may skip stored rows so it reads at most the latest `keep` rows after
# `after_ordinal`: the `keep + 1`-th row from the end of the record, or None when fewer than that remain. One
# bounded index walk from the end of the record, so the cost is `keep` and not the length of the record.
async def aload_transcript_row_floor(
    conn: AsyncConnection,
    agent_id: int,
    session_id: str,
    record: str,
    *,
    after_ordinal: int,
    keep: int,
) -> int | None:
    cursor = await conn.execute(
        "SELECT ordinal FROM agent_harness_transcript_rows "
        "WHERE agent_id = %s AND session_id = %s AND record = %s AND ordinal > %s "
        "ORDER BY ordinal DESC OFFSET %s LIMIT 1",
        (agent_id, session_id, record, after_ordinal, keep),
    )
    row = await cursor.fetchone()
    return None if row is None else _int_from_row(row)
