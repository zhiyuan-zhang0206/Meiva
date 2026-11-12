# meiva:note
# Database operations of the transcript-sourced timeline of an external harness (the writer in
# `agent/harness_timeline`): the batch append of projected timeline items, the lookup of the calls a sub-agent's
# items nest under, the stored read positions per transcript record, the whitelisted transcript rows and the
# bounded reads of what the writer needs to start again. The table of the items themselves, and the operations the
# graph path uses, are in `harness_messages.py`.
# meiva:link future/agent/transcript-timeline-source.md

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from psycopg import AsyncConnection

from base.agents.history.harness_messages import aget_harness_call_item_ids, call_id_of
from base.agents.history.timeline_item import TimelineItem
from base.db.rows import aquery_all


@dataclass(frozen=True)
class _ItemRow:
    item: object


def _int_from_row(row: tuple[object, ...] | None) -> int:
    if not row or row[0] is None:
        return 0
    val = row[0]
    return int(val) if isinstance(val, (int, str)) else 0


_INSERT_PLANNED_TIMELINE_ITEM = """
INSERT INTO agent_harness_messages
    (agent_id, seq, harness, item, inbound_id, created_at, source_key, projection_version, call_id)
VALUES (%s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, clock_timestamp()), %s, %s, %s)
ON CONFLICT (agent_id, source_key) WHERE source_key IS NOT NULL DO NOTHING
"""


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


# meiva:note
# One item of a batch for `aappend_planned_timeline_items`: the stable key of the transcript entry, the item, and
# the CLI's id of the call its sub-agent was started by when it is a sub-agent's (the item then nests under that
# call's item).
@dataclass(frozen=True, slots=True)
class PlannedTimelineItem:
    key: str
    item: TimelineItem
    parent_tool_use_id: str | None


# meiva:note
# Append a batch of items projected from transcript entries in one go, in the order given: one lock of the agent's
# row, one read of the next sequence number, one existence check of the keys, at most one lookup of the calls the
# sub-agent items nest under that are not known yet, and one insert of the whole batch (pipelined), whatever the
# batch's size and the length of the timeline. An item whose key the agent already has is skipped and gives no row
# and no sequence number (the sequence stays contiguous). `calls` is what the caller already knows of
# `tool_use_id -> item id` (a value of None is a call looked up and absent); the answer says what was learnt:
# the calls found in the store, the calls looked up and absent, and the calls this batch wrote. The caller holds
# the transaction and has taken its owner lock (`lock_inbound_owner`). Returns the items written, with their ids.
# meiva:link base/agents/history/docs/timeline/timeline.md
async def aappend_planned_timeline_items(
    conn: AsyncConnection,
    agent_id: int,
    harness: str,
    planned: Sequence[PlannedTimelineItem],
    *,
    calls: Mapping[str, str | None],
    projection_version: int,
) -> tuple[list[TimelineItem], dict[str, str | None]]:
    if not planned:
        return [], {}
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM agents_meta WHERE id = %s FOR UPDATE", (agent_id,))
        await cur.execute(
            "SELECT source_key FROM agent_harness_messages WHERE agent_id = %s AND source_key = ANY(%s)",
            (agent_id, [entry.key for entry in planned]),
        )
        existing: list[tuple[object, ...]] = await cur.fetchall()
        fresh = _not_present(planned, {str(row[0]) for row in existing})
        if not fresh:
            return [], {}
        found = await aget_harness_call_item_ids(
            conn, agent_id, sorted(_unknown_parents(fresh, calls))
        )
        learnt: dict[str, str | None] = {
            call: found.get(call) for call in _unknown_parents(fresh, calls)
        }
        await cur.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        first = _int_from_row(await cur.fetchone())
        written = _numbered(fresh, {**calls, **learnt}, learnt, first)
        await cur.executemany(
            _INSERT_PLANNED_TIMELINE_ITEM,
            [
                (
                    agent_id,
                    first + index,
                    harness,
                    _json(item),
                    item.inbound_id,
                    item.created_at,
                    key,
                    projection_version,
                    call_id_of(item),
                )
                for index, (key, item) in enumerate(written)
            ],
        )
    return [item for _, item in written], learnt


def _not_present(
    planned: Sequence[PlannedTimelineItem], present: set[str]
) -> list[PlannedTimelineItem]:
    fresh: list[PlannedTimelineItem] = []
    for entry in planned:
        if entry.key not in present:
            present.add(entry.key)
            fresh.append(entry)
    return fresh


def _unknown_parents(
    fresh: Sequence[PlannedTimelineItem], calls: Mapping[str, str | None]
) -> set[str]:
    written = {
        entry.item.tool_use_id
        for entry in fresh
        if entry.item.kind == "agent_code" and entry.item.tool_use_id
    }
    return {
        entry.parent_tool_use_id
        for entry in fresh
        if entry.parent_tool_use_id is not None
        and entry.parent_tool_use_id not in calls
        and entry.parent_tool_use_id not in written
    }


def _numbered(
    fresh: Sequence[PlannedTimelineItem],
    known: dict[str, str | None],
    learnt: dict[str, str | None],
    first: int,
) -> list[tuple[str, TimelineItem]]:
    numbered: list[tuple[str, TimelineItem]] = []
    for index, entry in enumerate(fresh):
        item = entry.item
        if entry.parent_tool_use_id is not None:
            item = item.model_copy(update={"parent_item_id": known.get(entry.parent_tool_use_id)})
        item = item.model_copy(update={"item_id": f"h{first + index}.0"})
        if item.kind == "agent_code" and item.tool_use_id:
            known[item.tool_use_id] = learnt[item.tool_use_id] = item.item_id
        numbered.append((entry.key, item))
    return numbered


def _json(item: TimelineItem) -> str:
    return json.dumps(item.model_dump(mode="json"))


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


_SET_TRANSCRIPT_POSITION = (
    "INSERT INTO agent_harness_transcript_positions "
    "(agent_id, session_id, record, position, rebuild_ordinal, cli_version) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (agent_id, session_id, record) DO UPDATE SET position = EXCLUDED.position, "
    "rebuild_ordinal = EXCLUDED.rebuild_ordinal, "
    "cli_version = COALESCE(EXCLUDED.cli_version, "
    "agent_harness_transcript_positions.cli_version), updated_at = clock_timestamp()"
)


async def aset_transcript_position(
    conn: AsyncConnection,
    agent_id: int,
    session_id: str,
    stored: StoredPosition,
    cli_version: str | None,
) -> None:
    await conn.execute(
        _SET_TRANSCRIPT_POSITION,
        (agent_id, session_id, stored.record, stored.position, stored.rebuild_ordinal, cli_version),
    )


async def aset_transcript_positions(
    conn: AsyncConnection,
    agent_id: int,
    session_id: str,
    stored: Sequence[StoredPosition],
    cli_version: str | None,
) -> None:
    if not stored:
        return
    async with conn.cursor() as cur:
        await cur.executemany(
            _SET_TRANSCRIPT_POSITION,
            [
                (agent_id, session_id, one.record, one.position, one.rebuild_ordinal, cli_version)
                for one in stored
            ],
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
