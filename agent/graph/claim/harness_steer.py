from collections.abc import Sequence

from agent.db import CLAIM_ACQUIRE_TIMEOUT_CAP_S, ClaimedInbound
from agent.ownership.inbound import lock_inbound_owner
from base.agents.context import MeivaContext
from base.agents.history.harness_messages import aappend_harness_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.inbound import InboundKind
from base.db.rows import IdRow, aquery_all
from base.db.transaction import async_write_transaction
from base.events.live.projection import TimelineSnapshot
from base.log import logger

from .harness_input import unconfirmed_input_items

STEERABLE_INBOUND_KINDS = (
    InboundKind.CHAT.value,
    InboundKind.SYSTEM_NOTE.value,
    InboundKind.HEARTBEAT.value,
)


def _validate_accepted_steer_items(
    rows: Sequence[tuple[object, ...]], accepted_items: Sequence[TimelineItem]
) -> None:
    chat_ids = {
        row_id
        for row_id, kind in rows
        if isinstance(row_id, int) and kind == InboundKind.CHAT.value
    }
    accepted_chat_ids = {
        item.inbound_id
        for item in accepted_items
        if item.kind == "inbound_chat" and item.inbound_id is not None
    }
    if not chat_ids.issubset(accepted_chat_ids):
        raise ValueError("acknowledged steer requires the matching inbound timeline item")
    note_count = len(rows) - len(chat_ids)
    marker_count = sum(1 for item in accepted_items if item.kind == "system_marker")
    if marker_count < note_count:
        raise ValueError("acknowledged steer requires a system_marker timeline item")


# meiva:note
# Reserve the contiguous FIFO batch of pending steerable inbounds at the head of
# the inbox for one provider steer request. Chats remain visible in the queued
# strip, and an ingress retry still matches the original payload. Any non-steerable
# barrier stops the batch before it so delivery order never overtakes a lifecycle
# or compaction boundary.
# meiva:link agent/graph/docs/harness-turn.md
async def reserve_harness_steer(ctx: MeivaContext, agent_id: int) -> list[ClaimedInbound]:
    pool = ctx.ops_pool
    if pool is None:
        return []
    timeout = min(CLAIM_ACQUIRE_TIMEOUT_CAP_S, pool.timeout)
    steerable = list(STEERABLE_INBOUND_KINDS)
    async with async_write_transaction(pool, timeout=timeout) as conn:
        _ = await conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{timeout:g}s",))
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        cursor = await conn.execute(
            "WITH first_barrier AS ("
            "  SELECT created_at, id FROM inbound_messages "
            "  WHERE agent_id=%s AND status='pending' AND NOT (kind = ANY(%s)) "
            "  ORDER BY created_at ASC, id ASC LIMIT 1"
            "), updated AS ("
            "  UPDATE inbound_messages SET claimed_at=clock_timestamp() "
            "  WHERE id IN ("
            "    SELECT m.id FROM inbound_messages m "
            "    WHERE m.agent_id=%s AND m.status='pending' AND m.claimed_at IS NULL "
            "      AND m.kind = ANY(%s) "
            "      AND NOT EXISTS ("
            "        SELECT 1 FROM first_barrier b "
            "        WHERE (m.created_at, m.id) >= (b.created_at, b.id)"
            "      ) "
            "    ORDER BY m.created_at ASC, id ASC "
            "    FOR UPDATE"
            "  ) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM inbound_messages active "
            "    WHERE active.agent_id=%s AND active.status='pending' "
            "      AND active.claimed_at IS NOT NULL"
            "  ) "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM inbound_messages control WHERE control.agent_id=%s "
            "    AND ((control.status='pending' AND control.kind='cancel') "
            "         OR (control.status IN ('pending','claimed') "
            "             AND control.kind IN ('restart','terminate')))"
            "  ) "
            "  RETURNING id,agent_id,content,kind,source,payload,created_at,claimed_at"
            ") "
            "SELECT id,agent_id,content,kind,source,payload,created_at,claimed_at "
            "FROM updated ORDER BY created_at ASC, id ASC",
            (agent_id, steerable, agent_id, steerable, agent_id, agent_id),
        )
        rows = await cursor.fetchall()
    return [ClaimedInbound.from_row(row) for row in rows]


# meiva:note
# A definite rejection releases every row in the reserved batch. A positive
# acknowledgement commits all timeline items and accompanying notes in the same
# transaction as inbox consumption. The caller publishes only after commit.
# meiva:link agent/graph/docs/harness-turn.md
async def finish_harness_steer(
    ctx: MeivaContext,
    agent_id: int,
    inbound_ids: Sequence[int],
    *,
    accepted_items: Sequence[TimelineItem] | None,
) -> list[TimelineItem]:
    pool = ctx.ops_pool
    if pool is None:
        raise RuntimeError("harness steering requires a durable inbox")
    ids = list(dict.fromkeys(inbound_ids))
    if not ids:
        return []
    if accepted_items is not None and len(accepted_items) < len(ids):
        raise ValueError("acknowledged steer requires the matching inbound timeline item")
    accepted = accepted_items is not None
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        cursor = await conn.execute(
            "UPDATE inbound_messages "
            "SET status = CASE WHEN %s THEN 'done' ELSE 'pending' END, "
            "    claimed_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END "
            "WHERE agent_id=%s AND id = ANY(%s) AND kind = ANY(%s) "
            "AND status='pending' AND claimed_at IS NOT NULL "
            "RETURNING id, kind",
            (accepted, accepted, agent_id, ids, list(STEERABLE_INBOUND_KINDS)),
        )
        rows = await cursor.fetchall()
        if len(rows) != len(ids):
            raise RuntimeError(f"harness steer reservation missing for inbounds {ids}")
        if accepted_items is not None:
            _validate_accepted_steer_items(rows, accepted_items)
        return await aappend_harness_timeline_items(
            conn, agent_id, ctx.harness.value, accepted_items or ()
        )


# meiva:note
# The next claim is a foreground handback. A surviving reservation means the
# provider might have read the message, so record uncertainty without replaying
# the inbound or presenting it as acknowledged. A committed acknowledgement
# already removed the reservation and wrote the accepted timeline item in one
# transaction.
# meiva:link agent/graph/docs/harness-turn.md
async def recover_harness_steer(
    ctx: MeivaContext, agent_id: int, *, publish: bool = True
) -> list[TimelineItem]:
    pool = ctx.ops_pool
    if pool is None:
        return []
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        rows = await aquery_all(
            conn,
            IdRow,
            "UPDATE inbound_messages SET status='done' "
            "WHERE agent_id=%s AND kind = ANY(%s) AND status='pending' AND claimed_at IS NOT NULL "
            "RETURNING id",
            (agent_id, list(STEERABLE_INBOUND_KINDS)),
        )
        ids = sorted(row.id for row in rows)
        if not ids:
            return []
        items = unconfirmed_input_items(ids, created_at=ctx.require_clock().now().isoformat())
        persisted = await aappend_harness_timeline_items(conn, agent_id, ctx.harness.value, items)
    logger.warning(
        "harness steer delivery could not be confirmed",
        agent_id=agent_id,
        inbound_ids=ids,
    )
    if publish and ctx.event_publisher is not None:
        ctx.event_publisher.emit(
            TimelineSnapshot(
                agent_id=agent_id,
                items=[item.model_dump() for item in persisted],
                msg_count=int(persisted[-1].item_id.partition(".")[0].removeprefix("h")) + 1,
            ).model_dump_json()
        )
    return persisted
