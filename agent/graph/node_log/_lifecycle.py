from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator, Sequence
from typing import Any

from langchain_core.messages import BaseMessage

from base.agents.history.timeline import build_timeline_items, tail_window
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.agents.observation.turn_progress import TurnProgress
from base.events.live.projection import TimelineSnapshot
from base.events.live.publisher import AgentEventPublisher
from base.log import logger

from ._plan import StallConfig, WindowMode, window_mode
from ._stall import stall_guard

# meiva:note
# Per-agent count of the messages the last published timeline snapshot covered. It is process
# state: one host serves many agents, so it outlives any one node and is keyed by agent id.
# meiva:link agent/graph/docs/node-lifecycle.meiva.okf.md
SNAPSHOT_CURSOR: dict[int, int] = {}

_NODE_EXIT_AGGREGATE: dict[int, list[dict[str, Any]]] = {}

_NODE_EXIT_AGGREGATE_CAP = 32


# meiva:link agent/graph/docs/node-lifecycle.meiva.okf.md
def flush_node_exit_aggregate(agent_id: int) -> None:
    entries = _NODE_EXIT_AGGREGATE.pop(agent_id, [])
    if not entries:
        return
    logger.info(
        "[node exits] {count}",
        event="node_exit",
        agent_id=agent_id,
        count=len(entries),
        nodes=entries,
    )


# meiva:link agent/graph/docs/node-lifecycle.meiva.okf.md
def _accumulate_node_exit(
    agent_id: int,
    node_name: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    entries = _NODE_EXIT_AGGREGATE.setdefault(agent_id, [])
    entries.append(
        {
            "node": node_name,
            "outcome": outcome,
            "duration_seconds": duration_seconds,
        }
    )
    if len(entries) >= _NODE_EXIT_AGGREGATE_CAP:
        flush_node_exit_aggregate(agent_id)


# meiva:note
# Wraps one graph node with its enter and exit records and the enter timeline snapshot. `stall`
# arms the diagnostic probe around the body and `timeline_limit` bounds a full-window snapshot.
# Normal and cancelled exits buffer per agent until the turn boundary flush; an exception exit
# logs at once, inside the `except`, so the record carries the live traceback.
# meiva:link agent/graph/docs/node-lifecycle.meiva.okf.md
@contextlib.asynccontextmanager
async def node_lifecycle(
    node_name: str,
    *,
    messages: Sequence[BaseMessage],
    event_publisher: AgentEventPublisher,
    agent_id: int,
    turn_progress: TurnProgress,
    stall: StallConfig,
    timeline_inputs: TimestampPolicy,
    timeline_limit: int,
    full_window: bool = False,
) -> AsyncGenerator[None]:
    turn_progress.mark(agent_id)
    with stall_guard(node_name, stall):
        cursor = SNAPSHOT_CURSOR.get(agent_id, 0)
        mode = window_mode(message_count=len(messages), cursor=cursor, full_window=full_window)
        if mode is WindowMode.FULL:
            items, msg_count = build_timeline_items(messages, inputs=timeline_inputs)
            window, _ = tail_window(items, timeline_limit)
        else:
            window, msg_count = build_timeline_items(messages, start=cursor, inputs=timeline_inputs)
        # meiva:note
        # An empty window publishes nothing and leaves the cursor, so a render that produced
        # nothing is retried by the next enter instead of being counted as covered.
        if window:
            event_publisher.emit(
                TimelineSnapshot(
                    agent_id=agent_id,
                    items=[it.model_dump() for it in window],
                    msg_count=msg_count,
                ).model_dump_json()
            )
            SNAPSHOT_CURSOR[agent_id] = len(messages)
        t0 = time.monotonic()
        logger.info(
            "[node enter {node}] msgs={msg_count}",
            event="node_enter",
            node=node_name,
            msg_count=msg_count,
        )
        try:
            yield
        except asyncio.CancelledError:
            _accumulate_node_exit(agent_id, node_name, "cancelled", time.monotonic() - t0)
            raise
        except BaseException as e:
            logger.opt(exception=True).info(
                "[node exit {node}] outcome=exception:{exc_name} {duration_seconds:.2f}s",
                event="node_exit",
                agent_id=agent_id,
                node=node_name,
                outcome=f"exception:{type(e).__name__}",
                exc_name=type(e).__name__,
                duration_seconds=time.monotonic() - t0,
            )
            raise
        else:
            _accumulate_node_exit(agent_id, node_name, "ok", time.monotonic() - t0)
