from collections.abc import Sequence

from agent.db import CLAIM_ACQUIRE_TIMEOUT_CAP_S, ClaimedInbound
from agent.ownership.inbound import lock_inbound_owner
from base.agents.context import MeivaContext
from base.agents.history.harness_messages import aappend_harness_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.db.rows import IdRow, aquery_all
from base.db.transaction import async_write_transaction
from base.events.live.projection import TimelineSnapshot
from base.log import logger

from .harness_input import unconfirmed_input_items


# meiva:note
# A pending chat with a pickup timestamp is reserved for a provider request. It
# remains visible in the queued strip, and an ingress retry still matches its
# original payload. Only the head of the inbox can enter the foreground turn.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def reserve_harness_steer(ctx: MeivaContext, agent_id: int) -> ClaimedInbound | None:
    pool = ctx.ops_pool
    if pool is None:
        return None
    timeout = min(CLAIM_ACQUIRE_TIMEOUT_CAP_S, pool.timeout)
    async with async_write_transaction(pool, timeout=timeout) as conn:
        _ = await conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{timeout:g}s",))
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        cursor = await conn.execute(
            "UPDATE inbound_messages SET claimed_at=clock_timestamp() "
            "WHERE id = (SELECT id FROM inbound_messages "
            "            WHERE agent_id=%s AND status='pending' "
            "            ORDER BY created_at, id LIMIT 1) "
            "AND status='pending' AND claimed_at IS NULL AND kind='chat' "
            "AND (source='user' OR source LIKE 'ui:page:%%') "
            "AND NOT (COALESCE(payload, '{}'::jsonb) ?| ARRAY['content_blocks', 'upload_files']) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM inbound_messages control WHERE control.agent_id=%s "
            "  AND ((control.status='pending' AND control.kind='cancel') "
            "       OR (control.status IN ('pending','claimed') "
            "           AND control.kind IN ('restart','terminate')))"
            ") "
            "RETURNING id,agent_id,content,kind,source,payload,created_at,claimed_at",
            (agent_id, agent_id),
        )
        row = await cursor.fetchone()
    return ClaimedInbound.from_row(row) if row is not None else None


# meiva:note
# A definite rejection releases the reservation. A positive acknowledgement
# commits the chat and any accompanying notes in the same transaction as inbox
# consumption. The caller publishes only after the transaction commits.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def finish_harness_steer(
    ctx: MeivaContext,
    agent_id: int,
    inbound_id: int,
    *,
    accepted_items: Sequence[TimelineItem] | None,
) -> list[TimelineItem]:
    pool = ctx.ops_pool
    if pool is None:
        raise RuntimeError("harness steering requires a durable inbox")
    if accepted_items is not None and not any(
        item.kind == "inbound_chat" and item.inbound_id == inbound_id for item in accepted_items
    ):
        raise ValueError("acknowledged steer requires the matching inbound timeline item")
    accepted = accepted_items is not None
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        cursor = await conn.execute(
            "UPDATE inbound_messages "
            "SET status = CASE WHEN %s THEN 'done' ELSE 'pending' END, "
            "    claimed_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END "
            "WHERE agent_id=%s AND id=%s AND kind='chat' "
            "AND status='pending' AND claimed_at IS NOT NULL",
            (accepted, accepted, agent_id, inbound_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"harness steer reservation missing for inbound {inbound_id}")
        return await aappend_harness_timeline_items(
            conn, agent_id, ctx.harness.value, accepted_items or ()
        )


# meiva:note
# The next claim is a foreground handback. A surviving reservation means the
# provider might have read the message, so record uncertainty without replaying
# the chat or presenting it as acknowledged. A committed acknowledgement already
# removed the reservation and wrote the accepted chat in one transaction.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
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
            "WHERE agent_id=%s AND kind='chat' AND status='pending' AND claimed_at IS NOT NULL "
            "RETURNING id",
            (agent_id,),
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
                msg_count=int(persisted[-1].item_id.partition(".")[0]) + 1,
            ).model_dump_json()
        )
    return persisted
