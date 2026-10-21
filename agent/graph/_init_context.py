# meiva:link agent/graph/docs/context-notes/context-notes.meiva.okf.md
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.impersonation_handoff import introduction_note
from agent.nodes import INIT_CONTEXT, NodeName
from agent.state import CapabilitiesState, ContextReset
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.agents.impersonation.notes import HandoffNotes
from base.api_contracts.agent_contract import Harness
from base.log import logger

from .claim import route_target
from .node_log import node_lifecycle
from .prompt.capabilities import indexed_skill_identifiers
from .prompt.context_notes import context_notes
from .prompt.system_prompt import build_system_prompt


# meiva:link agent/graph/docs/context-notes/context-notes.meiva.okf.md
async def init_context_node(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> Command[NodeName]:
    event_publisher = runtime.context.event_publisher
    assert event_publisher is not None, "init_context requires ctx.event_publisher"  # noqa: S101
    async with node_lifecycle(
        INIT_CONTEXT,
        messages=state.messages,
        event_publisher=event_publisher,
        agent_id=agent_id_from_config(config),
        turn_progress=runtime.context.turn_progress,
        read_stall_seconds=lambda: runtime.context.require_agent().read(
            "agent", "node_stall_dump_seconds"
        ),
        timeline_inputs=TimelineReadInputs(
            runtime.context.require_clock,
            lambda: runtime.context.require_agent().read("general", "message_timestamps"),
        ),
        limit_reader=lambda: runtime.context.require_agent().read(
            "display", "timeline_default_limit"
        ),
    ):
        reset = state.context_reset
        if state.messages:
            # History intact — nothing to establish. A parked tail here would
            # mean a requester cleared the history and something re-populated it
            # before this node ran; drop it rather than splicing it into the
            # middle of a live conversation.
            if reset.tail:
                logger.warning(
                    "[init-context] messages non-empty ({}) with a parked tail of {} — "
                    "dropping the tail",
                    len(state.messages),
                    len(reset.tail),
                )
                return Command[NodeName](
                    update={"context_reset": ContextReset()},
                    goto=route_target(runtime.context, reset.resume),
                )
            return Command[NodeName](goto=route_target(runtime.context, reset.resume))

        # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
        if runtime.context.harness is not Harness.MEIVA:
            return Command[NodeName](
                update={
                    "messages": list(reset.tail),
                    "harness_cursor": 0,
                    "context_reset": ContextReset(),
                },
                goto=route_target(runtime.context, reset.resume),
            )

        # Container mode (eval harness): no ops_pool, so no inbound queue, no
        # workspace, and nothing the notes describe. The head is the system
        # prompt alone, keeping an eval's context deterministic. Same signal
        # `_claim` uses for its container early-return.
        notes = (
            []
            if runtime.context.ops_pool is None
            else context_notes(runtime.context.plugin_registry(), runtime.context)
        )
        if state.impersonation_introduced:
            notes.append(
                introduction_note(
                    notes=HandoffNotes(
                        runtime.context.require_clock,
                        lambda: runtime.context.require_agent().read(
                            "general", "message_timestamps"
                        ),
                    )
                )
            )
        logger.info(
            "[init-context] establishing: {} note(s) + {} tail message(s), resume={}",
            len(notes),
            len(reset.tail),
            reset.resume,
        )
        # Record what `# Capabilities` covers BEFORE rendering it. The section is
        # frozen into the SystemMessage below while `meiva.skills` stays an uncached
        # live scan, so this snapshot is the only thing that can later tell the
        # two apart — see `agent/graph/hooks/capabilities.py`. Taken first on purpose:
        # a skill landing between the two calls is then rendered but not
        # recorded, so the drift check names it one extra time, where the reverse
        # order would mark it known and drop it silently until the next
        # compaction.
        indexed = indexed_skill_identifiers(runtime.context.require_agent().prompt)
        head: list[BaseMessage] = [
            SystemMessage(
                content=build_system_prompt(
                    runtime.context.plugin_registry(),
                    runtime.context.require_agent(),
                    catalog=runtime.context.require_catalog(),
                    agent_id=runtime.context.identity.agent_id
                    if runtime.context.identity is not None
                    else None,
                )
            ),
            *notes,
            *reset.tail,
        ]
        update: dict[str, Any] = {
            "messages": head,
            "capabilities": CapabilitiesState(indexed=indexed),
            # Clear the channel: the reset has been served. Written back as
            # the default so a later invocation cannot re-consume this tail.
            "context_reset": ContextReset(),
        }
        return Command[NodeName](update=update, goto=route_target(runtime.context, reset.resume))
