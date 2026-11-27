from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Literal

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph.claim.harness_inbox import restart_owed, settle_stale_interrupts
from agent.graph.claim.harness_interrupt import (
    HarnessTimeline,
    TimelineNotifications,
    announce_interrupt,
    await_foreground,
)
from agent.graph.claim.harness_read_sink import ReadTimelineSink
from agent.graph.claim.harness_run import ForegroundRun
from agent.graph.claim.lost_turn import report_lost_turn
from agent.graph.interrupt import (
    ModelInterruptedError,
    interrupt_halt,
)
from agent.graph.llm_errors import FatalLLMStreamError, HarnessTurnHaltedError
from agent.graph.node_log import StallConfig, node_lifecycle
from agent.nodes import CLAIM, HARNESS_TURN
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.history.transcript_timeline.record.harness import (
    record_location,
    session_file,
    unreadable_file,
)
from base.agents.messages.inbound import InterruptReason
from base.agents.recovery.breaker import HALT_REASON_QUOTA_EXHAUSTED, HALT_REASON_TURN_STALLED
from base.events.live.publisher import AgentEventPublisher
from base.lm.harness import (
    HarnessClosedError,
    HarnessSetupError,
    HarnessTurnFaultedError,
    HarnessTurnStalledError,
    TurnEngine,
    fault_is_quota_exhaustion,
    sanitize_error_text,
)
from base.lm.harness.registry import FileSessionRegistry
from base.log import logger
from base.paths import harness_sessions_dir, workspace_dir

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
        return await run_harness_turn(state, ctx, agent_id, event_publisher)


def _apply_wake_polarity(
    state: _state.AgentState,
    run: ForegroundRun,
    updates: dict[str, object],
) -> None:
    if state.non_human_wake and not run.protocol.non_human_wake:
        updates["non_human_wake"] = False


_CLEARED: dict[str, object] = {"harness_inputs": [], "harness_view": []}


def _is_quota_exhaustion(
    exc: HarnessTurnFaultedError | HarnessClosedError | HarnessSetupError,
) -> bool:
    outcome = exc.outcome if isinstance(exc, HarnessTurnFaultedError) else None
    return fault_is_quota_exhaustion(
        outcome=outcome, fallback_text=str(exc) if outcome is None else None
    )


# meiva:note
# The breaker's reason when the abort halts the agent instead of failing the turn: the harness ran out
# of quota, or the turn stalled (`HarnessTurnStalledError`). Any other abort is a failed turn.
def _halt_reason(
    exc: HarnessTurnFaultedError | HarnessClosedError | HarnessSetupError,
) -> str | None:
    if isinstance(exc, HarnessTurnStalledError):
        return HALT_REASON_TURN_STALLED
    if _is_quota_exhaustion(exc):
        return HALT_REASON_QUOTA_EXHAUSTED
    return None


# meiva:note
# Stop and say so: the aborted turn's inputs go back to the queue, the agent is halted until a new
# message (`harness_inbox.py::settle_halted_turn`), and the warning plus the turn's Error event are
# the alert. When the quota returns, or why a harness went silent, is not this code's guess to make.
def _build_halted_error(
    ctx: MeivaContext,
    agent_id: int,
    bridge: TurnEngine,
    exc: HarnessTurnFaultedError | HarnessClosedError | HarnessSetupError,
    reason: str,
    *,
    requeued: tuple[int, ...],
    after_output: bool = False,
) -> HarnessTurnHaltedError:
    harness = ctx.harness.value
    if isinstance(exc, HarnessTurnStalledError):
        detail = (
            sanitize_error_text(f"{harness} harness turn stalled: {exc}")
            or f"{harness} harness turn stalled"
        )
        msg = (
            "harness turn stalled after partial output without a reply; session retired and "
            "the agent halted without requeuing delivered inputs"
            if after_output
            else "harness turn stalled; session retired, inputs requeued and the agent halted "
            "until a new message"
        )
        logger.warning(
            msg,
            event="harness_turn_stalled",
            agent_id=agent_id,
            harness=harness,
            model=bridge.model,
            silent_seconds=round(exc.silent_seconds, 1),
            stall_seconds=exc.stall_seconds,
            requeued_inbound_ids=list(requeued),
            requeued_count=len(requeued),
            reason=detail,
        )
    else:
        detail = (
            sanitize_error_text(f"{harness} harness quota exhausted: {exc}")
            or f"{harness} harness quota exhausted"
        )
        msg = (
            "harness turn aborted due to quota exhaustion after delivering output; agent halted "
            "without requeuing delivered inputs"
            if after_output
            else "harness turn aborted due to quota exhaustion; inputs requeued and the agent "
            "halted until a new message"
        )
        logger.warning(
            msg,
            event="harness_quota_exhausted",
            agent_id=agent_id,
            harness=harness,
            model=bridge.model,
            requeued_inbound_ids=list(requeued),
            requeued_count=len(requeued),
            reason=detail,
        )
    return HarnessTurnHaltedError(detail, reason=reason, requeued_inbound_ids=requeued)


async def _settle_halted_turn(
    ctx: MeivaContext,
    agent_id: int,
    bridge: TurnEngine,
    run: ForegroundRun,
    exc: HarnessTurnFaultedError | HarnessClosedError | HarnessSetupError,
    reason: str,
) -> HarnessTurnHaltedError:
    turn_id = exc.turn_id if isinstance(exc, HarnessTurnFaultedError) else None
    requeued = await run.settle_halted_turn(turn_id=turn_id, reason=reason)
    return _build_halted_error(ctx, agent_id, bridge, exc, reason, requeued=requeued)


async def _finish_harness_abort(
    ctx: MeivaContext,
    agent_id: int,
    bridge: TurnEngine,
    projector: TimelineNotifications,
    run: ForegroundRun,
    exc: HarnessTurnFaultedError | HarnessClosedError | HarnessSetupError,
) -> HarnessTurnHaltedError | None:
    if not isinstance(exc, HarnessTurnFaultedError) or exc.retire_session:
        reason = (
            exc.retire_reason
            if isinstance(exc, HarnessTurnFaultedError) and exc.retire_reason
            else "session unavailable"
        )
        await bridge.retire(reason=reason)
    halt_reason = _halt_reason(exc)
    if halt_reason is not None and not run.protocol.has_delivered_output:
        return await _settle_halted_turn(ctx, agent_id, bridge, run, exc, halt_reason)

    produced_output = run.protocol.has_activity
    marked = await run.retire_unconfirmed()
    if produced_output:
        projector.post(partial(projector.sink.finish, error=str(exc)))
    elif marked:
        report_lost_turn(ctx, agent_id, marked, run.last_key, exc)
    if halt_reason is not None:
        # meiva:note
        # Requeuing delivered inputs and tripping the recovery breaker are separate decisions:
        # after partial output (tools or text deltas) without a completed reply, already-delivered
        # inputs cannot safely be re-executed, while the recovery breaker still halts automatic
        # wakes for both quota exhaustion and genuine pre-reply stalls.
        await run.record_halt(reason=halt_reason)
        return _build_halted_error(
            ctx,
            agent_id,
            bridge,
            exc,
            halt_reason,
            requeued=(),
            after_output=True,
        )
    return None


def _session_died(exc: HarnessTurnFaultedError | HarnessClosedError | HarnessSetupError) -> bool:
    if isinstance(exc, HarnessTurnFaultedError) and exc.outcome is not None:
        return False
    return _halt_reason(exc) is None


async def _cut_by_host_exit(
    ctx: MeivaContext,
    agent_id: int,
    exc: HarnessTurnFaultedError | HarnessClosedError | HarnessSetupError,
) -> bool:
    return ctx.host_exiting() and _session_died(exc) and await restart_owed(ctx, agent_id)


async def _settle_exit_cut(
    ctx: MeivaContext,
    agent_id: int,
    state: _state.AgentState,
    bridge: TurnEngine,
    projector: TimelineNotifications,
    run: ForegroundRun,
    exc: HarnessTurnFaultedError | HarnessClosedError | HarnessSetupError,
    extra_updates: dict[str, object],
) -> Command[HarnessGoto]:
    await bridge.retire(reason="session unavailable")
    produced_output = run.protocol.has_activity
    marked = await run.retire_unconfirmed()
    projector.post(partial(projector.sink.finish, interrupted=True))
    if marked and not produced_output:
        report_lost_turn(ctx, agent_id, marked, run.last_key, exc)
    logger.info(
        "harness session died during the host's graceful exit; the turn is handed to the "
        "restart the exit wrote ({detail})",
        agent_id=agent_id,
        harness=ctx.harness.value,
        retired_inputs=marked,
        detail=sanitize_error_text(str(exc)) or type(exc).__name__,
    )
    _apply_wake_polarity(state, run, extra_updates)
    return Command[HarnessGoto](
        update={**extra_updates, **interrupt_halt(InterruptReason.RESTART), **_CLEARED},
        goto=CLAIM,
    )


async def run_harness_turn(
    state: _state.AgentState,
    ctx: MeivaContext,
    agent_id: int,
    event_publisher: AgentEventPublisher,
    *,
    timeline: HarnessTimeline | None = None,
) -> Command[HarnessGoto]:
    bridge = ctx.require_harness_bridge()
    ctx.turn_progress.mark(agent_id)
    session_id = _session_of(ctx, agent_id, ctx.harness.value)
    sink = (
        timeline
        if timeline is not None
        else ReadTimelineSink(
            ctx,
            agent_id,
            event_publisher,
            non_human_wake=state.non_human_wake,
            session_id=session_id,
        )
    )
    projector = TimelineNotifications(agent_id, sink)
    run = ForegroundRun(
        ctx,
        agent_id,
        bridge,
        projector,
        state,
        event_publisher,
        check_completed_record=partial(_completed_record_error, ctx, agent_id, session_id),
        stall_seconds=ctx.require_agent().read("agent", "harness_turn_stall_seconds"),
    )
    await settle_stale_interrupts(ctx, agent_id)
    async with projector.running():
        return await _run_foreground(ctx, agent_id, state, bridge, projector, run)


async def _run_foreground(
    ctx: MeivaContext,
    agent_id: int,
    state: _state.AgentState,
    bridge: TurnEngine,
    projector: TimelineNotifications,
    run: ForegroundRun,
) -> Command[HarnessGoto]:
    extra_updates: dict[str, object] = {}

    try:
        await await_foreground(ctx, agent_id, bridge, projector, run.execute)
    except ModelInterruptedError as exc:
        await run.retire_unconfirmed()
        projector.post(partial(projector.sink.finish, interrupted=True))
        _apply_wake_polarity(state, run, extra_updates)
        return Command[HarnessGoto](
            update={**extra_updates, **interrupt_halt(exc.reason), **_CLEARED}, goto=CLAIM
        )
    except (HarnessTurnFaultedError, HarnessClosedError, HarnessSetupError) as exc:
        if (
            isinstance(exc, HarnessClosedError)
            and run.settled_for_background_exit
            and not _is_quota_exhaustion(exc)
        ):
            await bridge.retire(reason="session unavailable")
            projector.post(projector.sink.finish)
            logger.info(
                "harness session closed after delivering replies while waiting on background "
                "work; session retired and agent idles cleanly ({detail})",
                agent_id=agent_id,
                harness=ctx.harness.value,
                detail=sanitize_error_text(str(exc)) or type(exc).__name__,
            )
            _apply_wake_polarity(state, run, extra_updates)
            return Command[HarnessGoto](
                update={
                    **extra_updates,
                    "halted": True,
                    "harness_inputs": run.pending_inputs(),
                    "harness_view": [],
                },
                goto=CLAIM,
            )
        if await _cut_by_host_exit(ctx, agent_id, exc):
            return await _settle_exit_cut(
                ctx, agent_id, state, bridge, projector, run, exc, extra_updates
            )
        halted = await _finish_harness_abort(ctx, agent_id, bridge, projector, run, exc)
        if halted is not None:
            raise halted from exc
        detail = (
            sanitize_error_text(f"{ctx.harness.value} harness turn aborted: {exc}")
            or f"{ctx.harness.value} harness turn aborted"
        )
        raise FatalLLMStreamError(detail) from exc
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

    projector.post(partial(projector.sink.finish, interrupted=run.interrupted))
    _apply_wake_polarity(state, run, extra_updates)
    if run.interrupted:
        projector.post_terminal(partial(announce_interrupt, ctx, agent_id))
    return Command[HarnessGoto](
        update={
            **extra_updates,
            "halted": True,
            "harness_inputs": run.pending_inputs(),
            "harness_view": [],
        },
        goto=CLAIM,
    )


def _session_of(ctx: MeivaContext, agent_id: int, harness: str) -> Callable[[], str | None]:
    registry = FileSessionRegistry(harness_sessions_dir(home=ctx.require_meiva_home()))

    def current() -> str | None:
        live = getattr(ctx.harness_bridge, "session_id", None)
        if isinstance(live, str):
            return live
        record = registry.load(agent_id, harness=harness)
        return None if record is None else record.session_id

    return current


RECORD_END_WAIT_SECONDS = 2.0
RECORD_END_POLL_SECONDS = 0.25


# meiva:link agent/graph/docs/harness-turn.md
async def _completed_record_error(
    ctx: MeivaContext, agent_id: int, session_id: Callable[[], str | None]
) -> HarnessSetupError | None:
    harness = ctx.harness.value
    environ = ctx.require_environ()
    workspace = workspace_dir(agent_id, home=ctx.require_meiva_home())
    clock = ctx.require_clock()
    deadline = clock.monotonic() + RECORD_END_WAIT_SECONDS
    while True:
        sid = session_id()
        if (
            sid is not None
            and await asyncio.to_thread(session_file, harness, environ, workspace, sid) is not None
        ):
            return None
        if clock.monotonic() >= deadline:
            break
        await clock.asleep(RECORD_END_POLL_SECONDS)

    name = (
        None
        if sid is None
        else await asyncio.to_thread(
            unreadable_file, harness, environ, sid, packed_is_readable=False
        )
    )
    if name is not None:
        logger.warning(
            "the record of session {session_id} is in a form the live timeline does not read: {name}",
            event="harness_record_unreadable",
            agent_id=agent_id,
            session_id=sid,
            name=name,
        )
    where = record_location(harness, environ, workspace)
    return HarnessSetupError(
        f"the record of the {harness} session {sid or '(not started)'} was not found "
        f"in {where} or any other project directory; nothing of this turn can be shown or delivered"
    )
