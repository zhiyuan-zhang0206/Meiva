"""Agent-side half of chunk-triggered understanding: when to enqueue a chunk.

Two producers feed `understanding_chunk_jobs` (the rule and the queue are
`base/agents/history/hierarchy/chunks.py`; the consumer is the agent host's
loop):

- `due_chunk_update` — after every llm turn. A segment's first turn only
  records the baseline: the cut moves past the head (system prompt and the
  carried-over compact summary, both already understood) and its tokens
  become the reference, so the fixed head never counts as material. After
  that, once the provider-reported `input_tokens` exceed those at the
  segment's previous cut by
  `chunk_threshold` (`MEIVA_UNDERSTANDING_CHUNK_RATIO` x the agent model's soft compaction threshold), enqueue
  `[cut index, request length)` and return the `compact` update that moves
  the cut. A failed enqueue leaves the cut where it was, so the next turn
  covers the same stretch again.
- `enqueue_closing_chunk` — at compaction: the segment's remainder from the
  last cut to its end, tied to the boundary checkpoint that holds the segment.

Both are best-effort and silent when `MEIVA_UNDERSTANDING_ENABLED` is off; neither
may fail the agent's turn.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage
from psycopg_pool import AsyncConnectionPool

from agent.state import AgentState
from agent.state_channels import CompactState
from base import telemetry
from base.agents.history.hierarchy.chunks import chunk_threshold, enqueue_chunk
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.log import logger

from .chunk_turn import Baseline, plan_closing, plan_turn
from .snapshot_wait import SnapshotWait, read_time, snapshot_verdict

DEFAULT_SNAPSHOT_WAIT = SnapshotWait()


async def due_chunk_update(
    compact: CompactState,
    request: list[AnyMessage],
    final_msg: AIMessage,
    *,
    pool: AsyncConnectionPool | None,
    agent_id: int,
    agent: AgentSlices,
    catalog: ModelCatalog,
) -> dict[str, Any]:
    """The state update after one llm turn: `{}`, or the moved cut once a chunk is enqueued.

    `request` is the message list the turn sent (head included); `final_msg`
    carries the provider's usage for it.
    """
    if pool is None or not agent.read("agent", "understanding_enabled"):
        return {}
    plan = plan_turn(
        compact,
        request,
        final_msg,
        threshold=chunk_threshold(
            agent.brain.llm_model,
            agent.overrides,
            agent.read("agent", "understanding_chunk_ratio"),
            catalog=catalog,
        ),
    )
    if plan is None:
        return {}
    if isinstance(plan, Baseline):
        return {"compact": plan.state}
    enqueued = await enqueue_chunk(
        pool,
        agent_id,
        compact_version=compact.version,
        chunk=plan.chunk,
        end_msg_id=plan.end_msg_id,
    )
    return {"compact": plan.moved} if enqueued else {}


async def enqueue_closing_chunk(
    compact: CompactState,
    messages: list[AnyMessage],
    *,
    pool: AsyncConnectionPool | None,
    agent_id: int,
    boundary: str | None,
    understanding_enabled: bool,
) -> None:
    """Enqueue the closing segment's remainder at compaction (best-effort).

    `messages` is the segment as the compaction saw it; `boundary`
    is the checkpoint stamped for it (None = the stamp failed, nothing to anchor
    the read on, so nothing is enqueued).
    """
    if pool is None or boundary is None or not understanding_enabled:
        return
    closing = plan_closing(compact, messages)
    if closing is None:
        return
    await enqueue_chunk(
        pool,
        agent_id,
        compact_version=compact.version,
        chunk=closing.chunk,
        end_msg_id=closing.end_msg_id,
        boundary_checkpoint_id=boundary,
    )


_NEWEST_CHECKPOINT_TS = (
    "SELECT checkpoint->>'ts' FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = ''"
    " ORDER BY checkpoint_id DESC LIMIT 1"
)


async def newest_checkpoint_ts(pool: AsyncConnectionPool, agent_id: int) -> datetime | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_NEWEST_CHECKPOINT_TS, (str(agent_id),))
        row = await cur.fetchone()
    return None if row is None or not row[0] else datetime.fromisoformat(str(row[0]))


async def await_snapshot(
    pool: AsyncConnectionPool | None,
    state: AgentState | None,
    agent_id: int,
    *,
    understanding_enabled: bool,
    wait: SnapshotWait = DEFAULT_SNAPSHOT_WAIT,
) -> None:
    """Wait (bounded) until the newest checkpoint of the agent is at least as new as the `state`'s
    last message.

    Called by the compact paths right before the boundary is stamped: the stamped checkpoint is
    the full-snapshot record of the segment, and the segment's closing chunk is read from it, so a
    checkpoint one super-step behind would lose the segment's last turns for good (from the
    stitched history as well). A checkpoint is written after the messages of its step, so one whose
    time is not before the last message's read time (`message_read_time`: the pickup time of an
    injected message, else its creation) holds it. One cheap read per poll and a bounded sleep;
    nothing about the turn, the state or the request prefix changes. When the wait cannot succeed
    (it times out, or the last message has no usable time: none, or no timezone) the boundary is
    stamped anyway and `understanding_snapshot_lag` is emitted. Best-effort, silent only when
    understanding is off or there is no state.
    """
    messages: list[AnyMessage] = [] if state is None else list(state.messages)
    if pool is None or not messages or not understanding_enabled:
        return
    last = read_time(messages[-1])
    started = time.monotonic()
    while last is not None:
        try:
            newest = await newest_checkpoint_ts(pool, agent_id)
        except Exception:
            logger.opt(exception=True).warning("snapshot check failed for agent {}", agent_id)
            return
        verdict = snapshot_verdict(newest, last, time.monotonic() - started, wait)
        if verdict == "caught_up":
            return
        if verdict == "give_up":
            break
        await asyncio.sleep(wait.poll_seconds)
    logger.warning(
        "compaction of agent {agent} stamps its boundary before the checkpoint is known to hold "
        "the last message {message}",
        agent=agent_id,
        message=messages[-1].id,
    )
    telemetry.emit(
        "telemetry",
        "understanding_snapshot_lag",
        attributes={"agent_id": agent_id, "waited_seconds": round(time.monotonic() - started, 1)},
    )
