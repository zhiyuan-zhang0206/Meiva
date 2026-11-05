from collections.abc import Mapping, Sequence

from psycopg import AsyncConnection
from pydantic import StrictInt, TypeAdapter

from agent.ownership.inbound import lock_inbound_owner
from base.agents.context import MeivaContext
from base.agents.history.harness_messages import aappend_harness_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.kwargs import NoteTag
from base.db.rows import IdRow, aquery_all
from base.db.transaction import async_write_transaction

_TIMELINE_ITEMS = TypeAdapter(list[TimelineItem])
_CHAT_IDS = TypeAdapter(list[StrictInt])


# meiva:note
# Persist the staged timeline and consume the exact owned chat rows together.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def commit_harness_input(
    ctx: MeivaContext, agent_id: int, pending_input: Mapping[str, object] | None
) -> list[TimelineItem]:
    pool = ctx.ops_pool
    if pool is None or pending_input is None or "timeline_items" not in pending_input:
        return []
    items = _TIMELINE_ITEMS.validate_python(pending_input["timeline_items"])
    chat_ids = _CHAT_IDS.validate_python(pending_input["claimed_chat_ids"])
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        if chat_ids and len(await _consume_claimed(conn, agent_id, chat_ids)) != len(chat_ids):
            raise RuntimeError(f"harness input claim changed for agent {agent_id}")
        return await aappend_harness_timeline_items(conn, agent_id, ctx.harness.value, items)


# meiva:note
# Retire only the unresolved rows of a staged batch. If acceptance committed as
# cancellation arrived, the accepted chat already owns the timeline position.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def retire_unconfirmed_harness_input(
    ctx: MeivaContext, agent_id: int, pending_input: Mapping[str, object] | None
) -> list[TimelineItem]:
    pool = ctx.ops_pool
    if pool is None or pending_input is None or "claimed_chat_ids" not in pending_input:
        return []
    chat_ids = _CHAT_IDS.validate_python(pending_input["claimed_chat_ids"])
    if not chat_ids:
        return []
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        retired = set(await _consume_claimed(conn, agent_id, chat_ids))
        if not retired:
            return []
        items = unconfirmed_input_items(
            [inbound_id for inbound_id in chat_ids if inbound_id in retired],
            created_at=ctx.require_clock().now().isoformat(),
        )
        return await aappend_harness_timeline_items(conn, agent_id, ctx.harness.value, items)


def unconfirmed_input_items(inbound_ids: Sequence[int], *, created_at: str) -> list[TimelineItem]:
    return [
        TimelineItem(
            item_id="0.0",
            kind="system_marker",
            source=NoteTag.TURN_LOST.value,
            payload=f"Delivery of message {inbound_id} to the running turn could not be "
            "confirmed. It was retired from the queue to avoid sending it twice.",
            created_at=created_at,
            inbound_id=inbound_id,
        )
        for inbound_id in inbound_ids
    ]


async def _consume_claimed(conn: AsyncConnection, agent_id: int, chat_ids: list[int]) -> list[int]:
    rows = await aquery_all(
        conn,
        IdRow,
        "UPDATE inbound_messages SET status='done' "
        "WHERE agent_id=%s AND id=ANY(%s) AND kind='chat' AND status='claimed' "
        "RETURNING id",
        (agent_id, chat_ids),
    )
    return [row.id for row in rows]
