# meiva:note
# The plumbing around one invocation's foreground: the interrupt subscriptions, the interrupt request that
# runs beside it and the progress tick. It knows nothing about inputs; the foreground run is a callable.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from typing import Any

from agent.graph.claim.harness_activity import HarnessTurnProjector
from agent.graph.interrupt import interrupt_halt as interrupt_halt
from agent.graph.interrupt import interruptible_model, subscribe_interrupt
from base.agents.context import MeivaContext
from base.agents.messages.inbound import InterruptScope
from base.events.live.projection import Cancelled
from base.lm.harness.engine import TurnEngine
from base.log import logger

# meiva:note
# This tick keeps host custody alive while a harness owns the invocation. Provider
# activity is recorded separately by the timeline projector.
# meiva:link agent/graph/docs/harness-turn.md
PROGRESS_TICK_SECONDS = 60.0


async def await_foreground(
    ctx: MeivaContext,
    agent_id: int,
    bridge: TurnEngine,
    projector: HarnessTurnProjector,
    execute: Callable[[], Awaitable[None]],
) -> None:
    resources = ctx.hosted_resources
    async with (
        subscribe_interrupt(
            ctx.ops_pool, agent_id, resources=resources, scope=InterruptScope.LIFECYCLE
        ) as lifecycle,
        subscribe_interrupt(
            ctx.ops_pool, agent_id, resources=resources, scope=InterruptScope.FOREGROUND
        ) as interrupt,
    ):

        async def request_interrupt() -> None:
            await interrupt.wait()
            if error := await interrupt_engine(agent_id, bridge):
                await projector.record_control(f"Cancellation unconfirmed: {error}", error=True)

        async def go() -> None:
            await _with_interrupt(execute, request_interrupt, agent_id)

        await interruptible_model(
            _ticking(ctx.turn_progress.mark, agent_id, go(), projector), lifecycle
        )


# meiva:note
# A foreground interrupt sends a control request while the event reader remains alive. Only the harness's
# own terminal event ends a turn, so an interrupt changes nothing about when the invocation ends: it ends
# when everything handed over is settled and no turn runs. Inputs not yet in the context at that moment
# are still delivered and settled; they are delivered as a new turn, as in the built-in runtime.
# meiva:link agent/graph/docs/harness-turn.md
async def _with_interrupt(
    execute: Callable[[], Awaitable[None]],
    request_interrupt: Callable[[], Coroutine[Any, Any, None]],
    agent_id: int,
) -> None:
    outcome: BaseException | None = None
    async with asyncio.TaskGroup() as tasks:
        interrupt_task = tasks.create_task(
            request_interrupt(), name=f"harness-interrupt-{agent_id}"
        )
        try:
            await execute()
        except BaseException as error:
            outcome = error
        finally:
            interrupt_task.cancel()
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
    if outcome is not None:
        raise outcome


# meiva:note
# The waited work is captured and raised after the group closes, the way `interruptible_model` does
# it: a TaskGroup wraps a lone body error in an ExceptionGroup, and the node matches a faulted session on
# the error's own type, so the identity has to survive this scope. A cancelled wait keeps its
# CancelledError for the same reason.
# meiva:link agent/graph/docs/harness-turn.md
async def _ticking(
    mark: Callable[[int], None],
    agent_id: int,
    work: Awaitable[None],
    projector: HarnessTurnProjector,
) -> None:
    async def settle() -> BaseException | None:
        try:
            await work
        except BaseException as error:
            return error
        return None

    async def tick() -> None:
        while True:
            await asyncio.sleep(PROGRESS_TICK_SECONDS)
            mark(agent_id)

    async with asyncio.TaskGroup() as tasks:
        ticker = tasks.create_task(tick(), name=f"harness-progress-{agent_id}")
        checkpoints = tasks.create_task(projector.flush_text(), name=f"harness-timeline-{agent_id}")
        outcome = await settle()
        ticker.cancel()
        checkpoints.cancel()
        with suppress(asyncio.CancelledError):
            await ticker
        with suppress(asyncio.CancelledError):
            await checkpoints
    if outcome is not None:
        raise outcome


# meiva:note
# Tell the live view the turn ended. Its committed activities remain visible and
# the durable interrupt stays pending for the next claim pass.
# meiva:link agent/graph/docs/harness-turn.md
def announce_interrupt(ctx: MeivaContext, agent_id: int) -> None:
    assert ctx.event_publisher is not None, "interrupted turn needs ctx.event_publisher"  # noqa: S101
    ctx.event_publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())


# meiva:note
# An interrupt that cannot be delivered is reported, never raised: the durable interrupt row is still
# pending and claim dispatches it on the next pass, while a raise here would turn the user's stop into
# a failed turn. Process observations are diagnostics. The foreground ends only when the executor
# reports a terminal outcome.
# meiva:link agent/graph/docs/harness-turn.md
async def interrupt_engine(agent_id: int, bridge: TurnEngine) -> str | None:
    try:
        report = await bridge.interrupt()
    except Exception as exc:
        logger.warning(
            "[{label}] {body}",
            label="harness",
            event="harness_interrupt_failed",
            body=f"interrupting agent {agent_id}'s harness turn raised: {exc!r}",
        )
        return str(exc) or type(exc).__name__
    logger.info(
        "[{label}] {body}",
        label="harness",
        event="harness_interrupt",
        body=f"requested interruption for agent {agent_id}'s harness turn",
        agent_id=agent_id,
        settled=getattr(report, "settled", None),
        turn_id=getattr(report, "turn_id", None),
    )
    return None
