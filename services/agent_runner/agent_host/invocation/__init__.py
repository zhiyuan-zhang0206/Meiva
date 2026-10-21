"""Pending invocation state and the host's database-only failure boundary."""

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import settle_checkpoint
from agent.impersonation_handoff import flush_checkpoint, resume_note_pending
from agent.ownership.hosted import apply_hosted_lifecycle
from agent.state import BaseAgentState
from agent.turn.runloop import PendingTurnFailure, publish_turn_failure, settle_turn_failure
from agent.turn.trace_checkpoint import attach_trace_checkpoint_ref
from base.agents.context import MeivaContext
from base.agents.impersonation.notes import HandoffNotes
from base.agents.observation.relay_supervision import RelaySupervision
from base.db import Database
from base.events.live.bus import EventBus
from base.log import logger
from services.agent_runner.agent_host.db_recovery import database_phase
from services.agent_runner.agent_host.invocation.checkpoints import TurnCheckpoints
from services.agent_runner.agent_host.runtime import TurnOutcome


@dataclass
class PendingWorkResult:
    """The completed invocation and its in-process database settlement phase."""

    result: dict[str, object]
    checkpoint_flushed: bool = False
    trace_attached: bool = False


# meiva:note
# Settle a pending external handoff checkpoint, without activating a new lease.
async def settle_handoff_before_invocation(
    graph: CompiledStateGraph[BaseAgentState, MeivaContext, BaseAgentState, BaseAgentState],
    ctx: MeivaContext,
    agent_id: int,
    *,
    fresh_input: bool,
    db: Database,
    bus: EventBus,
    relays: RelaySupervision,
    notes: HandoffNotes,
) -> bool:
    async with database_phase():
        await settle_checkpoint(
            graph,
            db,
            bus,
            agent_id,
            relays,
            activate_accepted=False,
            incarnation=ctx.original_incarnation,
            notes=notes,
            read_local_machine=lambda: ctx.require_machine().name(),
        )
        if not fresh_input or ctx.original_incarnation is None:
            return False
        snapshot = await graph.aget_state({"configurable": {"thread_id": str(agent_id)}})
        state = BaseAgentState.model_validate(snapshot.values)
        return not state.halted and resume_note_pending(state)


async def finish_pending_failure(
    graph: CompiledStateGraph[BaseAgentState, MeivaContext, BaseAgentState, BaseAgentState],
    checkpointer: object,
    agent_id: int,
    ctx: MeivaContext,
    config: RunnableConfig,
    pending: PendingTurnFailure,
) -> TurnOutcome:
    """Finish the original abort's durable writes without another invocation."""
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


async def finish_completed_invocation(
    pool: AsyncConnectionPool,
    checkpoints: TurnCheckpoints,
    agent_id: int,
    ctx: MeivaContext,
    pending: PendingWorkResult,
    drop_agent: Callable[[int], None],
    kill_shell_sessions: Callable[[int, int], None],
    wake_resumed: Callable[[int], object],
    *,
    db: Database,
    bus: EventBus,
    relays: RelaySupervision,
    notes: HandoffNotes,
) -> TurnOutcome | None:
    """Settle the original completed work through its admission's checkpoint view."""
    # Correlate the original trace only after its checkpoint is durable.
    async with database_phase():
        if not pending.checkpoint_flushed:
            await flush_checkpoint(checkpoints.saver, agent_id)
            pending.checkpoint_flushed = True
        if not pending.trace_attached:
            await attach_trace_checkpoint_ref(checkpoints.graph, ctx, agent_id)
            pending.trace_attached = True
    if pending.result["exit_requested"] or pending.result["restart_requested"]:
        incarnation = ctx.require_original_incarnation(agent_id)
        drop_agent(agent_id)
        # meiva:note
        # After a restart claim `halted` is the claim's verdict on the agent it found:
        # set, it was idle; clear, its turn was unfinished and the apply queues the note
        # that continues it. Nothing else reads the turn's progress here.
        # meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md
        resume_turn = bool(pending.result["restart_requested"]) and not pending.result["halted"]
        async with database_phase():
            kind = await apply_hosted_lifecycle(
                pool,
                incarnation,
                bus=bus,
                resume_turn=resume_turn,
                kill_shell_sessions=kill_shell_sessions,
            )
        logger.info(
            "hosted lifecycle return settled",
            agent_id=agent_id,
            generation=str(incarnation.generation),
            command_kind=kind,
        )
        if kind == "restart" and resume_turn:
            # meiva:note
            # The apply committed the note that continues the turn. This host's scheduler
            # starts that turn as soon as this one ends, instead of the pending scan finding
            # it up to its interval later. A host that is exiting refuses the wake and the
            # next host's scan takes the note.
            # meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
            wake_resumed(agent_id)
        return TurnOutcome(exited=kind == "terminate", crashed=False)
    if pending.result["turn_idle"]:
        async with database_phase():
            await settle_checkpoint(
                checkpoints.graph,
                db,
                bus,
                agent_id,
                relays,
                incarnation=ctx.original_incarnation,
                notes=notes,
                read_local_machine=lambda: ctx.require_machine().name(),
            )
        return TurnOutcome(exited=False, crashed=False)
    return None
