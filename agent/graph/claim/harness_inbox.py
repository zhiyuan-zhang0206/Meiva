# meiva:note
# The inbox side of external-harness delivery: reserve the head batch, settle it, give it back. One
# state expresses "handed to the harness, not settled": `status='claimed'`, for every deliverable kind.
# A row leaves it only through the harness's own facts: in the context (done, with its timeline entries
# in the same transaction), or refused or lost (back to `pending` at its original queue position).
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

from collections.abc import Sequence
from typing import LiteralString

from psycopg import sql

from agent.db import CLAIM_ACQUIRE_TIMEOUT_CAP_S, ClaimedInbound
from agent.graph.claim.harness_foreground import Handed
from agent.ownership.inbound import lock_inbound_owner
from base.agents.context import MeivaContext
from base.agents.history.harness_messages import aappend_harness_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.inbound import InboundKind
from base.agents.messages.kwargs import NoteTag
from base.db.rows import IdRow, aquery_all
from base.db.transaction import async_write_transaction

DELIVERABLE_INBOUND_KINDS = (
    InboundKind.CHAT.value,
    InboundKind.SYSTEM_NOTE.value,
    InboundKind.HEARTBEAT.value,
)

_COLUMNS: LiteralString = "id,agent_id,content,kind,source,payload,created_at,claimed_at"

# meiva:note
# The contiguous FIFO batch of pending deliverable rows at the head of the inbox. Any other pending kind
# is a barrier: delivery never overtakes a lifecycle or compaction boundary. Nothing is handed over while
# a cancel is pending or a restart or terminate is pending or claimed.
# meiva:link agent/graph/docs/harness-turn.md
_HEAD_IDS: LiteralString = (
    "SELECT m.id FROM inbound_messages m "
    "WHERE m.agent_id=%(agent)s AND m.status='pending' AND m.kind = ANY(%(kinds)s) "
    "  AND NOT EXISTS ("
    "    SELECT 1 FROM inbound_messages b WHERE b.agent_id=%(agent)s AND b.status='pending' "
    "      AND NOT (b.kind = ANY(%(kinds)s)) AND (b.created_at, b.id) <= (m.created_at, m.id)"
    "  ) "
    "  AND NOT EXISTS ("
    "    SELECT 1 FROM inbound_messages control WHERE control.agent_id=%(agent)s "
    "    AND ((control.status='pending' AND control.kind='cancel') "
    "         OR (control.status IN ('pending','claimed') "
    "             AND control.kind IN ('restart','terminate')))"
    "  )"
)


async def peek_head_batch(ctx: MeivaContext, agent_id: int) -> list[ClaimedInbound]:
    pool = ctx.ops_pool
    if pool is None:
        return []
    async with pool.connection() as conn:
        cursor = await conn.execute(
            sql.SQL(
                "SELECT {columns} FROM inbound_messages WHERE id IN ({head}) "
                "ORDER BY created_at ASC, id ASC"
            ).format(columns=sql.SQL(_COLUMNS), head=sql.SQL(_HEAD_IDS)),
            {"agent": agent_id, "kinds": list(DELIVERABLE_INBOUND_KINDS)},
        )
        return [ClaimedInbound.from_row(row) for row in await cursor.fetchall()]


async def reserve_head_batch(ctx: MeivaContext, agent_id: int) -> list[ClaimedInbound]:
    pool = ctx.ops_pool
    if pool is None:
        return []
    timeout = min(CLAIM_ACQUIRE_TIMEOUT_CAP_S, pool.timeout)
    async with async_write_transaction(pool, timeout=timeout) as conn:
        _ = await conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{timeout:g}s",))
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        cursor = await conn.execute(
            sql.SQL(
                "UPDATE inbound_messages SET status='claimed', claimed_at=clock_timestamp() "
                "WHERE id IN ({head}) RETURNING {columns}"
            ).format(columns=sql.SQL(_COLUMNS), head=sql.SQL(_HEAD_IDS)),
            {"agent": agent_id, "kinds": list(DELIVERABLE_INBOUND_KINDS)},
        )
        rows = await cursor.fetchall()
        if rows:
            _ = await conn.execute(
                "UPDATE agents_meta SET wake_suppressed_until=NULL, wake_suppress_reason=NULL "
                "WHERE id=%s AND wake_suppressed_until IS NOT NULL",
                (agent_id,),
            )
    claimed = [ClaimedInbound.from_row(row) for row in rows]
    claimed.sort(key=lambda item: (item.created_at, item.id))
    return claimed


# meiva:note
# ACK 2: the rows become done and their timeline entries are written in one transaction, so the pending
# strip and the timeline never disagree. The caller publishes only after the commit.
async def commit_handed(
    ctx: MeivaContext, agent_id: int, handed: Sequence[Handed]
) -> list[TimelineItem]:
    pool = ctx.ops_pool
    if pool is None:
        return []
    ids = [h.inbound_id for h in handed if h.inbound_id is not None]
    items = [item for h in handed for item in h.items]
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        if ids:
            rows = await aquery_all(
                conn,
                IdRow,
                "UPDATE inbound_messages SET status='done' "
                "WHERE agent_id=%s AND id=ANY(%s) AND kind=ANY(%s) AND status='claimed' "
                "RETURNING id",
                (agent_id, ids, list(DELIVERABLE_INBOUND_KINDS)),
            )
            if len(rows) != len(ids):
                raise RuntimeError(f"harness delivery claim changed for agent {agent_id}: {ids}")
        return await aappend_harness_timeline_items(conn, agent_id, ctx.harness.value, items)


# meiva:note
# A refused or lost input goes back to the queue: `pending` with no claim time, so it sorts at its
# original position and the next reservation hands it over again, ahead of anything newer.
async def requeue_handed(ctx: MeivaContext, agent_id: int, handed: Sequence[Handed]) -> None:
    pool = ctx.ops_pool
    ids = [h.inbound_id for h in handed if h.inbound_id is not None]
    if pool is None or not ids:
        return
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        _ = await conn.execute(
            "UPDATE inbound_messages SET status='pending', claimed_at=NULL "
            "WHERE agent_id=%s AND id=ANY(%s) AND kind=ANY(%s) AND status='claimed'",
            (agent_id, ids, list(DELIVERABLE_INBOUND_KINDS)),
        )


# meiva:note
# The session failed with inputs in flight: whether they arrived is unknown, so they are consumed and
# marked rather than sent a second time. Reconciling them against the harness's durable record (and
# re-delivering the ones that are not there) is the restart-and-crash step of the delivery unification;
# this is its single entry point.
# meiva:link agent/graph/docs/harness-turn.md
async def retire_unconfirmed(
    ctx: MeivaContext, agent_id: int, handed: Sequence[Handed]
) -> list[TimelineItem]:
    pool = ctx.ops_pool
    ids = [h.inbound_id for h in handed if h.inbound_id is not None]
    if pool is None or not ids:
        return []
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        rows = await aquery_all(
            conn,
            IdRow,
            "UPDATE inbound_messages SET status='done' "
            "WHERE agent_id=%s AND id=ANY(%s) AND kind=ANY(%s) AND status='claimed' RETURNING id",
            (agent_id, ids, list(DELIVERABLE_INBOUND_KINDS)),
        )
        if not rows:
            return []
        items = unconfirmed_input_items(
            sorted(row.id for row in rows), created_at=ctx.require_clock().now().isoformat()
        )
        return await aappend_harness_timeline_items(conn, agent_id, ctx.harness.value, items)


def unconfirmed_input_items(inbound_ids: Sequence[int], *, created_at: str) -> list[TimelineItem]:
    return [
        TimelineItem(
            item_id="0.0",
            kind="system_marker",
            source=NoteTag.TURN_LOST.value,
            payload=f"Delivery of message {inbound_id} to the harness could not be "
            "confirmed. It was retired from the queue to avoid sending it twice.",
            created_at=created_at,
            inbound_id=inbound_id,
        )
        for inbound_id in inbound_ids
    ]
