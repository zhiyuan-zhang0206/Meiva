# meiva:note
# Committed messages become harness timeline rows: the one write path shared by the turn's own
# settlement, an interrupted turn's partial reply, and a turn that died before it said anything.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage

from base.agents.context import MeivaContext
from base.agents.history.harness_messages import aappend_harness_timeline_items
from base.agents.history.timeline import build_timeline_items
from base.db.transaction import async_write_transaction

__all__ = ["append_turn_timeline"]


async def append_turn_timeline(
    ctx: MeivaContext, agent_id: int, messages: Sequence[BaseMessage]
) -> None:
    if not messages or ctx.ops_pool is None:
        return
    items, _ = build_timeline_items(messages, inputs=ctx.timestamp_policy())
    async with async_write_transaction(ctx.ops_pool) as pool_conn:
        await aappend_harness_timeline_items(pool_conn, agent_id, ctx.harness.value, items)
