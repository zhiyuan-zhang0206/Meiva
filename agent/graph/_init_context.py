# meiva:link agent/graph/docs/context-notes/context-notes.meiva.okf.md
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md

from __future__ import annotations

from typing import Any, cast

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
from base.lm.harness.handoff import ClusterFact, Handoff
from base.lm.harness.wiring import HANDOFF_INSTRUCTION, handoff_for
from base.log import logger
from base.paths import workspace_dir

from .claim import route_target
from .node_log import node_lifecycle
from .prompt.capabilities import disabled_by_sdk_config, indexed_skill_identifiers
from .prompt.context_notes import (
    context_notes,
    harness_context_notes,
    machine_clause,
    own_label,
)
from .prompt.system_prompt import build_harness_system_prompt, build_system_prompt


def _cluster_facts(ctx: MeivaContext) -> tuple[ClusterFact, ...]:
    if ctx.ops_pool is None or ctx.identity is None:
        machine = machine_clause(ctx)
        return (ClusterFact(machine, "agent-runner"),) if machine else ()
    try:
        with ctx.sql.cursor() as cur:
            cur.execute(
                "SELECT name, role, description FROM machines WHERE paused_at IS NULL ORDER BY name"
            )
            rows = cur.fetchall()
    except Exception:
        logger.opt(exception=True).warning(
            "Failed to query machines table for handoff cluster facts; using local machine clause"
        )
        machine = machine_clause(ctx)
        return (ClusterFact(machine, "agent-runner"),) if machine else ()
    facts: list[ClusterFact] = []
    for name, role, description in rows:
        if isinstance(role, list):
            role_str = ", ".join(str(r) for r in cast(list[object], role)) or "host"
        else:
            role_str = str(role or "host")
        desc = f"{role_str} ({description})" if description else role_str
        facts.append(ClusterFact(str(name), desc))
    return tuple(facts)


def _build_grounded_handoff(ctx: MeivaContext, *, agent_id: int) -> Handoff:
    slices = ctx.require_agent()
    catalog = ctx.require_catalog()
    extensions = ctx.plugin_registry()
    prompt_body = build_harness_system_prompt(
        extensions,
        slices,
        agent_id=agent_id,
        catalog=catalog,
    )
    notes = harness_context_notes(extensions, ctx)
    instruction_parts = [p for p in (prompt_body, *notes, HANDOFF_INSTRUCTION) if p]
    instruction = "\n\n".join(instruction_parts)
    label = (
        own_label(ctx, agent_id) if ctx.ops_pool is not None and ctx.identity is not None else None
    )
    cluster = _cluster_facts(ctx)
    handoff = handoff_for(
        ctx.harness,
        agent_id=agent_id,
        machine=machine_clause(ctx) or "",
        workspace=workspace_dir(agent_id),
        instruction=instruction,
        label=label,
        cluster=cluster,
        contributions=tuple(hc for _, hc in extensions.harness_contributions(ctx.harness)),
    )
    filtered_entries = tuple(
        entry
        for entry in handoff.entries
        if entry.name == "meiva"
        or not disabled_by_sdk_config(
            entry.name.removeprefix("meiva."),
            slices.prompt.sdk_disable,
        )
    )
    return Handoff(
        agent_id=handoff.agent_id,
        instruction=handoff.instruction,
        machine=handoff.machine,
        workspace=handoff.workspace,
        label=handoff.label,
        cluster=handoff.cluster,
        entries=filtered_entries,
        overlaps=handoff.overlaps,
        discipline=handoff.discipline,
    )


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
            agent_id = agent_id_from_config(config)
            handoff = _build_grounded_handoff(runtime.context, agent_id=agent_id)
            set_handoff = getattr(runtime.context.harness_bridge, "set_handoff", None)
            if callable(set_handoff):
                set_handoff(handoff)
            return Command[NodeName](
                update={
                    "messages": [SystemMessage(content=handoff.render()), *reset.tail],
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
