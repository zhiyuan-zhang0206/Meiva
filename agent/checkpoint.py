from __future__ import annotations

from collections.abc import Awaitable

from langgraph.graph.state import CompiledStateGraph

from agent.state import BaseAgentState
from base.agents.context import MeivaContext

__all__ = ["AgentGraph", "flush_checkpoint"]

AgentGraph = CompiledStateGraph[BaseAgentState, MeivaContext, BaseAgentState, BaseAgentState]


# meiva:note
# Flush the optional buffered saver without probing dynamic attributes.
async def flush_checkpoint(checkpointer: object, agent_id: int) -> None:
    attributes: dict[str, object] = getattr(checkpointer, "__dict__", {})
    flush = attributes.get("_meiva_nstep_flush")
    if callable(flush):
        pending = flush(str(agent_id))
        if isinstance(pending, Awaitable):
            await pending
