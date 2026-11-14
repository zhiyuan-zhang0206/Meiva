from __future__ import annotations

from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph.claim.harness_activity import log_turn_usage
from agent.graph.claim.harness_inbox import settle_stale_interrupts
from agent.graph.claim.harness_interrupt import announce_interrupt, await_foreground
from agent.graph.claim.harness_read_sink import ReadTimelineSink, session_of
from agent.graph.claim.harness_run import ForegroundRun
from agent.graph.claim.lost_turn import report_lost_turn
from agent.graph.interrupt import (
    ModelInterruptedError,
    interrupt_halt,
)
from agent.graph.llm_errors import FatalLLMStreamError
from agent.graph.node_log import StallConfig, node_lifecycle
from agent.nodes import CLAIM, HARNESS_TURN
from base.agents.context import MeivaContext, agent_id_from_config
from base.events.live.publisher import AgentEventPublisher
from base.lm.harness import (
    HarnessClosedError,
    HarnessSetupError,
    HarnessTurnFaultedError,
)
from base.log import logger

HarnessGoto = Literal["claim"]


# meiva:note
# The graph invocation stays until nothing is left to settle: every input handed to the harness has
# entered its context (or gone back to the queue) and no turn is running. A turn ending settles
# nothing, and Claude Code reports an input that missed the last tool boundary about a second after
# the original turn, as a turn of its own. The invocation is also where that settlement is written, so it
# must still be here when the report arrives. Inputs are delivered one path: the head of the inbox,
# rendered, handed to `deliver()` after subscribing; idle or mid-turn is the harness's business.
# meiva:link agent/graph/docs/harness-turn.md
async def harness_turn_node(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> Command[HarnessGoto]:
    ctx = runtime.context
    event_publisher = ctx.event_publisher
    assert event_publisher is not None, "harness_turn requires ctx.event_publisher"  # noqa: S101
    agent_id = agent_id_from_config(config)
    async with node_lifecycle(
        HARNESS_TURN,
        messages=state.messages,
        event_publisher=event_publisher,
        agent_id=agent_id,
        turn_progress=ctx.turn_progress,
        stall=StallConfig(ctx.require_agent().read("agent", "node_stall_dump_seconds")),
        timeline_inputs=ctx.timestamp_policy(),
        timeline_limit=ctx.require_agent().read("display", "timeline_default_limit"),
        publish_timeline=False,
    ):
        return await _run_harness_turn(state, ctx, agent_id, event_publisher)


def _apply_wake_polarity(
    state: _state.AgentState,
    projector: ReadTimelineSink,
    updates: dict[str, Any],
) -> None:
    if state.non_human_wake and not projector.non_human_wake:
        updates["non_human_wake"] = False


_CLEARED: dict[str, Any] = {"harness_inputs": [], "harness_view": []}


async def _run_harness_turn(
    state: _state.AgentState,
    ctx: MeivaContext,
    agent_id: int,
    event_publisher: AgentEventPublisher,
) -> Command[HarnessGoto]:
    bridge = ctx.require_harness_bridge()
    ctx.turn_progress.mark(agent_id)
    extra_updates: dict[str, Any] = {}

    projector = ReadTimelineSink(
        ctx,
        agent_id,
        event_publisher,
        non_human_wake=state.non_human_wake,
        session_id=session_of(ctx, agent_id, ctx.harness.value),
    )
    run = ForegroundRun(ctx, agent_id, bridge, projector, state, event_publisher)
    await settle_stale_interrupts(ctx, agent_id)

    try:
        await await_foreground(ctx, agent_id, bridge, projector, run.execute)
    except ModelInterruptedError as exc:
        await run.retire_unconfirmed()
        await projector.finish(interrupted=True)
        _apply_wake_polarity(state, projector, extra_updates)
        return Command[HarnessGoto](
            update={**extra_updates, **interrupt_halt(exc.reason), **_CLEARED}, goto=CLAIM
        )
    except (HarnessTurnFaultedError, HarnessClosedError, HarnessSetupError) as exc:
        if isinstance(exc, HarnessTurnFaultedError) and exc.outcome is not None:
            log_turn_usage(ctx, agent_id, bridge.model, exc.outcome)
        if not isinstance(exc, HarnessTurnFaultedError) or exc.outcome is None:
            await bridge.retire(reason="session unavailable")
        produced_output = projector.has_activity
        marked = await run.retire_unconfirmed()
        if produced_output:
            await projector.finish(error=str(exc))
        elif marked:
            report_lost_turn(ctx, agent_id, marked, run.last_key, exc)
        raise FatalLLMStreamError(f"{ctx.harness.value} harness turn aborted: {exc}") from exc
    except Exception:
        # meiva:note
        # Whatever else goes wrong (a pool timeout past its retries, a failed commit), what was
        # handed over gets an explicit outcome before the error propagates: the host's abort pass
        # would otherwise consume the claimed rows without consulting the harness's record. The
        # original error is the one raised; a failure to record the outcome is logged beside it.
        # meiva:link agent/graph/docs/harness-foreground.md
        try:
            await run.retire_unconfirmed()
        except Exception:
            logger.opt(exception=True).warning(
                "could not record the outcome of inputs handed over before a failure",
                event="harness_unsettled_outcome_failed",
                agent_id=agent_id,
            )
        raise

    await projector.finish(interrupted=run.interrupted)
    _apply_wake_polarity(state, projector, extra_updates)
    if run.interrupted:
        announce_interrupt(ctx, agent_id)
    return Command[HarnessGoto](update={**extra_updates, "halted": True, **_CLEARED}, goto=CLAIM)
