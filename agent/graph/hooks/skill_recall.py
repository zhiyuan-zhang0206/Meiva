from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Overwrite

from agent import state as _state
from agent.graph.hooks.compact import auto_compact_will_fire
from agent.graph.prompt.capabilities import recall_candidates
from agent.graph.recall.skill_recall import SkillRecall, peer_gate_passes, recall_skills
from agent.hooks import Hook
from agent.messages import last_agent_inbound_text, tail_has_skill_recall_inbound
from agent.state import SkillRecallState
from base.agents.context import MeivaContext, agent_id_from_config
from base.log import logger

_LABEL = "skill-recall"


# meiva:link agent/graph/docs/skill-recall.meiva.okf.md
class SkillRecallHook(Hook):
    # meiva:note
    # No-op when the feature is off, in an eval run, when the tail holds no person or schedule
    # inbound (or, with peer messages on, no peer message the peer gate passes), when
    # auto-compact is predicted, when every candidate is already read, offered or preloaded,
    # when the call fails or exceeds its deadline, or when no skill reaches the threshold. A
    # compaction resets the window record with or without a note.
    async def __call__(
        self,
        state: _state.AgentState,
        runtime: Runtime[MeivaContext],
        config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        agent = runtime.context.require_agent()
        if not agent.skill_recall.skill_recall_enabled or agent.sandbox.eval_isolation:
            return None

        version = state.compact.version
        update: dict[str, Any] = {}
        read: set[str] = set(state.skill_reads)
        prompted: set[str] = set(state.skill_recall.prompted)
        if version > state.skill_recall.window:
            # A compaction summarized away the earlier note and the earlier reads.
            read, prompted = set[str](), set[str]()
            update["skill_reads"] = Overwrite([])
            update["skill_recall"] = SkillRecallState(window=version)

        eligible = tail_has_skill_recall_inbound(state.messages)
        if not eligible and agent.skill_recall.skill_recall_peer_messages:
            peer_text = last_agent_inbound_text(state.messages)
            if peer_text is not None:
                eligible = await peer_gate_passes(
                    peer_text,
                    agent=agent,
                    catalog=runtime.context.require_catalog(),
                    agent_id=agent_id_from_config(config),
                )
        recall = (
            await self._offer(state, runtime, config, excluded=read | prompted)
            if eligible
            else None
        )
        if recall is not None:
            update["messages"] = [recall.note]
            update["skill_recall"] = SkillRecallState(
                prompted=prompted | recall.identifiers, window=version
            )
        return update or None

    async def _offer(
        self,
        state: _state.AgentState,
        runtime: Runtime[MeivaContext],
        config: RunnableConfig,
        *,
        excluded: set[str],
    ) -> SkillRecall | None:
        agent = runtime.context.require_agent()
        catalog = runtime.context.require_catalog()
        if auto_compact_will_fire(state, agent, catalog=catalog):
            logger.info(
                "[{label}] {body}",
                label=_LABEL,
                body="defer: auto-compact predicted, skipping skill recall this turn",
                event="skill_recall",
            )
            return None
        candidates = [c for c in recall_candidates(agent.prompt) if c.identifier not in excluded]
        if not candidates:
            return None
        deadline = agent.skill_recall.skill_recall_deadline_seconds
        try:
            return await asyncio.wait_for(
                recall_skills(
                    state.messages,
                    candidates,
                    agent=agent,
                    catalog=catalog,
                    agent_id=agent_id_from_config(config),
                ),
                timeout=deadline,
            )
        except TimeoutError:
            logger.info(
                "[{label}] {body}",
                label=_LABEL,
                body=f"skip: recall exceeded its {deadline:g}s deadline, no skill note this turn",
                event="skill_recall",
            )
            return None
