"""Settle an ended hosted turn and consume its claimed inputs without redelivery. Crash reaping marks termination and publishes diagnostics; it never queues a recovery wake."""

from __future__ import annotations

import asyncio

from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import settle_and_stamp_turn
from agent.ownership.inbound import RuntimeOwnershipLostError
from agent.startup import consume_claimed_inbounds
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.stop_timing import CANCEL_UNWIND_TIMEOUT_S
from services.agent_runner.agent_host.db_recovery import database_phase
from services.agent_runner.agent_host.runtime import TurnOutcome
from services.agent_runner.agent_host.stall_guard import HostRestartRequiredError

__all__ = [
    "close_hosted_turn",
    "reconcile_inbounds_after_abort",
    "reconcile_inbounds_after_turn",
]


async def close_hosted_turn(
    pool: AsyncConnectionPool,
    control_pool: AsyncConnectionPool,
    bus: EventBus,
    incarnation: RuntimeIncarnation,
    outcome: TurnOutcome,
) -> None:
    """Settle the finished turn, then dispose its claimed rows (the abort
    pass, or a finished turn's own pass).

    Both consume passes need the turn's own live incarnation (its lease fence).
    A crash is only stamped here; the periodic corpse reaper terminates it once
    its grace window elapses."""
    settlement = await settle_and_stamp_turn(
        control_pool,
        incarnation,
        bus=bus,
        exited=outcome.exited,
        crashed=outcome.crashed,
    )
    if outcome.failure_report is not None:
        if settlement.stamp is not None and settlement.stamp.applied and settlement.settled:
            report, outcome.failure_report = outcome.failure_report, None
            await report()
        else:
            logger.error(
                "turn Error withheld: its fatal cutoff or owner settlement is unproven",
                agent_id=incarnation.agent_id,
            )
    if outcome.aborted:
        await reconcile_inbounds_after_abort(
            pool,
            incarnation,
        )
    elif not outcome.crashed:
        await reconcile_inbounds_after_turn(
            pool,
            incarnation,
        )


async def reconcile_inbounds_after_abort(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
) -> None:
    """Consume an aborted turn's claims under its original owner lock.

    Claimed chats are consumed as interrupted with unknown results and never
    returned to pending. A missing lease, unresolved resources or write failure
    leaves the claims for a later admission to consume, without executing the
    old work.
    """
    agent_id = incarnation.agent_id
    try:
        async with database_phase():
            await consume_claimed_inbounds(
                pool, agent_id, incarnation=incarnation, unknown_execution=True
            )
    except RuntimeOwnershipLostError:
        logger.warning(
            "host abort reconcile skipped: runtime ownership already replaced — "
            "the replacement disposes the claimed rows",
            event="host_abort_reconcile_skipped",
            agent_id=agent_id,
            reason="ownership_lost",
        )
    except Exception:
        logger.opt(exception=True).warning(
            "host abort reconcile failed — the next cold admission retries it",
            event="host_abort_reconcile_failed",
            agent_id=agent_id,
        )


async def reconcile_inbounds_after_turn(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
) -> None:
    """Dispose the finished turn's claimed inbounds at its settlement point.

    Every claim is consumed, including an interrupted one whose result is
    unknown, so it cannot automatically replay execution. Pending new inputs
    stay available. The original incarnation and settled resource fence
    serialize this pass against another claim.
    """
    agent_id = incarnation.agent_id
    try:
        async with database_phase():
            await consume_claimed_inbounds(pool, agent_id, incarnation=incarnation)
    except RuntimeOwnershipLostError:
        # Expected fail-closed path (the event declares tier="noise"): the
        # replacement runtime disposes the rows, so this is INFO, not a
        # WARNING-level anomaly (2026-10-03 triage).
        logger.info(
            "turn inbound reconcile skipped: runtime ownership already replaced — "
            "the replacement disposes the claimed rows",
            event="host_turn_reconcile_skipped",
            agent_id=agent_id,
            reason="ownership_lost",
        )
    except Exception:
        logger.opt(exception=True).warning(
            "turn inbound reconcile failed — the next cold admission retries it",
            event="host_turn_reconcile_failed",
            agent_id=agent_id,
        )


async def wait_shielded_task[T](task: asyncio.Task[T]) -> bool:
    """Observe one cleanup task within a fixed bound, including repeated cancellation."""
    cancelled = False
    deadline = asyncio.get_running_loop().time() + CANCEL_UNWIND_TIMEOUT_S
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise HostRestartRequiredError("hosted cleanup exceeded its finite deadline")
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            cancelled = True
    return cancelled
