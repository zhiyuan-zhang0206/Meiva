# meiva:note
# Pending invocation state and the host's database-only failure boundary.

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import assert_never

from langchain_core.runnables import RunnableConfig
from loguru import logger

from agent.impersonation import process_host, settle_checkpoint
from agent.impersonation_handoff import AgentGraph, flush_checkpoint, handoff_resume_pending
from agent.turn.runloop import PendingTurnFailure, publish_turn_failure, settle_turn_failure
from agent.turn.trace_checkpoint import attach_trace_checkpoint_ref
from base.agents.context import MeivaContext
from base.agents.messages.timestamp_policy import TimestampSource
from base.agents.observation.relay_supervision import RelaySupervision
from base.db.protocols import AsyncPool, DatabaseLike
from base.events.live.bus import EventBus
from services.agent_runner.agent_host import TurnOutcome
from services.agent_runner.agent_host.db_recovery import database_phase
from services.agent_runner.agent_host.invocation.checkpoints import TurnCheckpoints
from services.agent_runner.agent_host.invocation.completion_decisions import (
    ApplyLifecycle,
    CarryOn,
    SettleIdle,
    handoff_resume_wanted,
    lifecycle_follow_up,
    plan_completion,
)
from services.agent_runner.agent_host.lifecycle.shells import apply_lifecycle_command


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
class HandoffSettlement:
    db: DatabaseLike
    bus: EventBus
    relays: RelaySupervision
    timestamps: TimestampSource
    local_machine: str
    home: Path
    rng: random.Random
    environ: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class InvocationHooks:
    settlement: HandoffSettlement
    drop_agent: Callable[[int], None]
    wake_resumed: Callable[[int], object]


# meiva:note
# Settle a pending external handoff checkpoint, without activating a new lease.
async def settle_handoff_before_invocation(
    graph: AgentGraph,
    ctx: MeivaContext,
    agent_id: int,
    *,
    fresh_input: bool,
    settlement: HandoffSettlement,
) -> bool:
    async with database_phase():
        await settle_checkpoint(
            graph,
            settlement.db,
            settlement.bus,
            agent_id,
            settlement.relays,
            activate_accepted=False,
            incarnation=ctx.original_incarnation,
            timestamps=settlement.timestamps,
            host=process_host(
                settlement.local_machine,
                home=settlement.home,
                environ=settlement.environ,
                rng=settlement.rng,
                clock=settlement.timestamps.clock,
            ),
        )
        if not handoff_resume_wanted(
            fresh_input=fresh_input, has_original_incarnation=ctx.original_incarnation is not None
        ):
            return False
        return await handoff_resume_pending(graph, agent_id)


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
            async with database_phase():
                kind = await apply_lifecycle_command(
                    pool,
                    incarnation,
                    bus=hooks.settlement.bus,
                    resume_turn=resume_turn,
                    home=hooks.settlement.home,
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
            async with database_phase():
                await settle_checkpoint(
                    checkpoints.graph,
                    hooks.settlement.db,
                    hooks.settlement.bus,
                    agent_id,
                    hooks.settlement.relays,
                    incarnation=ctx.original_incarnation,
                    timestamps=hooks.settlement.timestamps,
                    host=process_host(
                        hooks.settlement.local_machine,
                        home=hooks.settlement.home,
                        environ=hooks.settlement.environ,
                        rng=hooks.settlement.rng,
                        clock=hooks.settlement.timestamps.clock,
                    ),
                )
            return TurnOutcome(exited=False, crashed=False)
        case CarryOn():
            return None
        case _:
            assert_never(plan.then)
