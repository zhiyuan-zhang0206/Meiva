# meiva:note
# The held path's one owed graph write, retried on a budget inside the wake that ends a
# takeover (#5468). A settlement that fails there leaves the agent's next request refused
# with nobody left to push it: no turn runs on that path, so the failure would surface only
# as a silent agent. Three attempts are synchronous on purpose — the wake already holds the
# incarnation and single-flight, and anything that outlived it would race the next dispatch
# on the same row. Each failed attempt stamps the crash marker so a hard kill mid-retry still
# leaves evidence for the heartbeat and the reaper; a later success clears that stamp.
# A lost ownership is never retried: it cannot be won back, and the caller's crash accounting
# is the honest outcome.
#
# The budget is the event loop's own timeout, not a deadline this code computes: the wake
# must not read the clock to bound an effect it already owns. It covers the receipt's write
# too, so a failure that lands after the receipt has to be told apart from one that landed
# before it — see `_fail_closed`.
# meiva:link docs/conventions/agents/agent-impersonation.md

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from agent.impersonation import (
    ImpersonationError,
    clear_turn_fatal,
    process_host,
    settle_checkpoint,
)
from agent.impersonation_handoff import AgentGraph
from agent.ownership.hosted import settle_hosted_runtime, stamp_turn_fatal
from base.agents.messages.timestamp_policy import TimestampSource
from base.agents.observation.relay_supervision import RelaySupervision
from base.db import Database
from base.deploy.progress_timeout import (
    HANDOFF_SETTLE_RETRY_BUDGET_S,
    HANDOFF_SETTLE_RETRY_INTERVAL_S,
)
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.runtime import HostStats

HANDOFF_SETTLE_ATTEMPTS = 3


@dataclass(frozen=True)
class HeldSettle:
    graph: AgentGraph
    db: Database
    bus: EventBus
    relays: RelaySupervision
    pool: AsyncConnectionPool
    timestamps: TimestampSource
    machine: str
    stats: HostStats
    rng: random.Random
    # meiva:note
    # Whether the receipt this wake owed has landed; the failure path asks it, and the host
    # hands over the database read (`agent.impersonation.handoff_landed`) rather than this
    # module reaching for one. It is the only injected collaborator here: `settle_checkpoint`,
    # `clear_turn_fatal` and `process_host` stay direct imports, because only this one has to
    # answer differently in a test of the window that lands after the write — the test supplies
    # the answer the committed thread would have given, and nothing else is replaced.
    receipt_landed: Callable[[AsyncConnectionPool, int], Awaitable[bool]]
    budget_s: float = HANDOFF_SETTLE_RETRY_BUDGET_S


async def settle_owed_handoff(
    held: HeldSettle, agent_id: int, incarnation: RuntimeIncarnation
) -> None:
    try:
        async with asyncio.timeout(held.budget_s):
            await _settle_attempts(held, agent_id, incarnation)
    except ImpersonationError:
        raise
    except Exception as error:
        # meiva:note
        # Neither a timeout nor an exhausted retry says whether the write landed: the receipt
        # commits in a thread, and the await after it is where a cancel, or a failure in the
        # wake that follows the receipt, still surfaces. `_fail_closed` asks the database first.
        if await _fail_closed(held, agent_id, incarnation, error):
            return
        raise


async def _settle_attempts(
    held: HeldSettle, agent_id: int, incarnation: RuntimeIncarnation
) -> None:
    delay = HANDOFF_SETTLE_RETRY_INTERVAL_S
    for attempt in range(1, HANDOFF_SETTLE_ATTEMPTS + 1):
        try:
            await settle_checkpoint(
                held.graph,
                held.db,
                held.bus,
                agent_id,
                held.relays,
                activate_accepted=False,
                incarnation=incarnation,
                timestamps=held.timestamps,
                host=process_host(held.machine, rng=held.rng, clock=held.timestamps.clock),
            )
        except ImpersonationError:
            raise
        except Exception as error:
            await stamp_turn_fatal(held.pool, incarnation)
            if attempt == HANDOFF_SETTLE_ATTEMPTS:
                raise
            held.stats.handoff_settle_retries += 1
            logger.warning(
                "held-path handoff settle failed; retrying inside this wake",
                event="hosted_handoff_settle_retry",
                agent_id=agent_id,
                attempt=attempt,
                delay_s=delay,
                error_class=type(error).__name__,
            )
            await held.timestamps.clock.asleep(delay)
            delay *= 3
        else:
            await clear_turn_fatal(held.pool, incarnation)
            return


# meiva:note
# Fail closed, not open — but only once the failure is known to be real. `settle_checkpoint`
# never changes the row's status, so a stamp would land on a row that may already be settled.
# So the first thing this does is ask the database whether the receipt landed; when it did, the
# wake records `hosted_handoff_settle_raced`, clears the marker an earlier attempt stamped, and
# reports success instead of marking a healthy agent crash-dead.
# meiva:link docs/conventions/agents/agent-impersonation.md
async def _fail_closed(
    held: HeldSettle,
    agent_id: int,
    incarnation: RuntimeIncarnation,
    error: Exception,
) -> bool:
    if await _receipt_landed(held, agent_id, incarnation):
        logger.warning(
            "the settle failed after its receipt landed; not marking the row",
            event="hosted_handoff_settle_raced",
            agent_id=agent_id,
            error_class=type(error).__name__,
        )
        return True
    held.stats.handoff_settle_failures += 1
    logger.error(
        "held-path handoff settle exhausted its retries; failing closed",
        event="hosted_handoff_settle_failed",
        agent_id=agent_id,
        budget_s=held.budget_s,
        error_class=type(error).__name__,
    )
    try:
        await stamp_turn_fatal(held.pool, incarnation)
    except Exception:
        logger.opt(exception=True).warning(
            "corpse marker stamp failed after a failed settle",
            event="corpse_stamp_failed",
            agent_id=agent_id,
        )
    try:
        await settle_hosted_runtime(held.pool, incarnation, bus=held.bus)
    except Exception:
        logger.opt(exception=True).warning(
            "settling a crash-marked row to idling failed; the reaper cannot see it",
            event="hosted_settle_failed",
            agent_id=agent_id,
        )
    return False


# meiva:note
# The one question a failure leaves open: did the write it interrupted commit? A read failure is
# not evidence either way, so it falls back to failing closed — the safe direction — and says so
# in the log. A landed receipt also voids the marker an earlier attempt stamped.
async def _receipt_landed(held: HeldSettle, agent_id: int, incarnation: RuntimeIncarnation) -> bool:
    try:
        landed = await held.receipt_landed(held.pool, agent_id)
    except Exception:
        logger.opt(exception=True).warning(
            "could not read whether the receipt landed after a settle failure",
            event="hosted_handoff_settle_read_failed",
            agent_id=agent_id,
        )
        return False
    if not landed:
        return False
    try:
        await clear_turn_fatal(held.pool, incarnation)
    except Exception:
        logger.opt(exception=True).warning(
            "clearing the marker after a landed receipt failed",
            event="hosted_turn_corpse_clear_failed",
            agent_id=agent_id,
        )
    return True
