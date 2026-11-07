# meiva:note
# Pending invocation state and the host's database-only failure boundary.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import assert_never

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from loguru import logger

from agent.checkpoint import AgentGraph, flush_checkpoint
from agent.turn.runloop import PendingTurnFailure, publish_turn_failure, settle_turn_failure
from agent.turn.trace_checkpoint import attach_trace_checkpoint_ref
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import ReconstructionScope
from base.db.protocols import AsyncPool
from base.events.live.bus import EventBus
from services.agent_runner.agent_host import TurnOutcome
from services.agent_runner.agent_host.db_recovery import database_phase
from services.agent_runner.agent_host.invocation.completion_decisions import (
    ApplyLifecycle,
    CarryOn,
    SettleIdle,
    lifecycle_follow_up,
    plan_completion,
)
from services.agent_runner.agent_host.lifecycle.shells import apply_lifecycle_command


# meiva:note
# A scoped read view over the host's original saver and compiled graph, passed through one hosted
# admission as explicit resources.
@dataclass(frozen=True)
class TurnCheckpoints:
    saver: AsyncPostgresSaver
    graph: AgentGraph
    reconstruction: ReconstructionScope[AsyncPostgresSaver] | None = None

    def bind(self, scope: ReconstructionScope[AsyncPostgresSaver] | None) -> TurnCheckpoints:
        if scope is None:
            return self
        reader = scope.reader()
        return TurnCheckpoints(reader, self.graph.copy({"checkpointer": reader}), scope)


# meiva:note
# The completed invocation and its in-process database settlement phase.
class PendingWorkResult:
    def __init__(
        self,
        result: dict[str, object],
        *,
        checkpoint_flushed: bool = False,
        trace_attached: bool = False,
    ) -> None:
        self.result = result
        self.checkpoint_flushed = checkpoint_flushed
        self.trace_attached = trace_attached


@dataclass(frozen=True)
class InvocationHooks:
    drop_agent: Callable[[int], None]
    wake_resumed: Callable[[int], object]
    home: Path
    bus: EventBus | None = None


# meiva:note
# Finish the original abort's durable writes without another invocation.
async def finish_pending_failure(
    graph: AgentGraph,
    checkpointer: object,
    agent_id: int,
    ctx: MeivaContext,
    config: RunnableConfig,
    pending: PendingTurnFailure,
) -> TurnOutcome:
    async with database_phase():
        await settle_turn_failure(graph, checkpointer, config, ctx, agent_id, pending)
        await attach_trace_checkpoint_ref(graph, ctx, agent_id)
    return TurnOutcome(
        exited=False,
        crashed=True,
        aborted=True,
        failure_report=(
            partial(publish_turn_failure, pending, ctx.require_bus()) if pending.errors else None
        ),
    )


# meiva:note
# Settle the original completed work through its admission's checkpoint view.
# Correlate the original trace only after its checkpoint is durable.
async def finish_completed_invocation(
    pool: AsyncPool,
    checkpoints: TurnCheckpoints,
    agent_id: int,
    ctx: MeivaContext,
    pending: PendingWorkResult,
    hooks: InvocationHooks,
) -> TurnOutcome | None:
    plan = plan_completion(
        pending.result,
        checkpoint_flushed=pending.checkpoint_flushed,
        trace_attached=pending.trace_attached,
    )
    async with database_phase():
        if plan.flush_checkpoint:
            await flush_checkpoint(checkpoints.saver, agent_id)
            pending.checkpoint_flushed = True
        if plan.attach_trace:
            await attach_trace_checkpoint_ref(checkpoints.graph, ctx, agent_id)
            pending.trace_attached = True
    match plan.then:
        case ApplyLifecycle(resume_turn=resume_turn):
            incarnation = ctx.require_original_incarnation(agent_id)
            hooks.drop_agent(agent_id)
            bus = hooks.bus or ctx.require_bus()
            async with database_phase():
                kind = await apply_lifecycle_command(
                    pool,
                    incarnation,
                    bus=bus,
                    resume_turn=resume_turn,
                    home=hooks.home,
                )
            logger.info(
                "hosted lifecycle return settled",
                agent_id=agent_id,
                generation=str(incarnation.generation),
                command_kind=kind,
            )
            follow_up = lifecycle_follow_up(kind, resume_turn=resume_turn)
            if follow_up.wake_resumed:
                hooks.wake_resumed(agent_id)
            return TurnOutcome(exited=follow_up.exited, crashed=False)
        case SettleIdle():
            return TurnOutcome(exited=False, crashed=False)
        case CarryOn():
            return None
        case _:
            assert_never(plan.then)
