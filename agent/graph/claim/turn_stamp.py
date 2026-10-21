# meiva:note
# The completed-turn stamp: `last_active_at`, `last_message_text`, the corpse marker and
# the permanent-reject streak move together, and both turn drivers write them — the
# native `llm` node and the external-harness `harness_turn` node. It lives here rather
# than in either node because `agent/graph/llm` already reaches `claim`, so a claim ->
# llm import would close a package cycle (#5511).
# meiva:link agent/graph/llm/docs/turn-records.meiva.okf.md

from __future__ import annotations

from base.agents.context import MeivaContext
from base.db.transaction import async_write_transaction
from base.log import logger


async def persist_last_active(ctx: MeivaContext, agent_id: int, text: str) -> None:
    ctx.turn_progress.mark(agent_id)
    if ctx.ops_pool is None:
        return
    try:
        async with async_write_transaction(ctx.ops_pool) as conn, conn.cursor() as cur:
            if text:
                await cur.execute(
                    "UPDATE agents_meta SET last_active_at = now(), last_message_text = %s, "
                    "last_turn_fatal_at = NULL, permanent_reject_streak = 0, last_permanent_reject_reason = NULL "
                    "WHERE id = %s",
                    (text, agent_id),
                )
            else:
                await cur.execute(
                    "UPDATE agents_meta SET last_active_at = now(), last_turn_fatal_at = NULL, "
                    "permanent_reject_streak = 0, last_permanent_reject_reason = NULL "
                    "WHERE id = %s",
                    (agent_id,),
                )
    except Exception:
        logger.warning(
            "[{label}] {body}",
            label="last-msg",
            event="last_msg",
            body=f"failed to persist last_active_at / last_message_text for agent {agent_id}",
        )
