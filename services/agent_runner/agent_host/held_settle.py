# meiva:note
# The held path's one owed graph write, retried on a budget inside the wake that ends a
# takeover (#5468). A settlement that fails there leaves the agent's next request refused
# with nobody left to push it: no turn runs on that path, so the failure would surface only
# as a silent agent. Three attempts are synchronous on purpose — the wake already holds the
# incarnation and single-flight, and anything that outlived it would race the next dispatch
# on the same row. Each failed attempt stamps the crash marker so a hard kill mid-retry still
# leaves evidence for the heartbeat and the reaper; a later success clears only that stamp.
# A lost ownership is never retried: it cannot be won back, and the caller's crash accounting
# is the honest outcome.
#
# The budget is the event loop's own timeout, not a deadline this code computes: the wake
# must not read the clock to bound an effect it already owns. It covers the receipt's write
# too, so a timeout that lands after the receipt still fails closed — the row is marked and
# idled, and the next completed turn clears the marker.
# meiva:link docs/conventions/agents/agent-impersonation.md

from __future__ import annotations

import asyncio
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


async def settle_owed_handoff(
    held: HeldSettle, agent_id: int, incarnation: RuntimeIncarnation
) -> None:
    try:
        async with asyncio.timeout(HANDOFF_SETTLE_RETRY_BUDGET_S):
            await _settle_attempts(held, agent_id, incarnation)
    except ImpersonationError:
        raise
    except Exception as error:
        await _fail_closed(held, agent_id, incarnation, error)
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
                host=process_host(held.machine),
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
            await asyncio.sleep(delay)
            delay *= 3
        else:
            await clear_turn_fatal(held.pool, incarnation)
            return


# meiva:note
# Fail closed, not open: the row is marked crash-dead and settled to idling so the
# heartbeat's narrow exception can re-check it inside the reap grace. Without both writes the
# row would stay `running` with no marker, which no scanner looks at.
# meiva:link docs/conventions/agents/agent-impersonation.md
async def _fail_closed(
    held: HeldSettle,
    agent_id: int,
    incarnation: RuntimeIncarnation,
    error: Exception,
) -> None:
    held.stats.handoff_settle_failures += 1
    logger.error(
        "held-path handoff settle exhausted its retries; failing closed",
        event="hosted_handoff_settle_failed",
        agent_id=agent_id,
        budget_s=HANDOFF_SETTLE_RETRY_BUDGET_S,
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
