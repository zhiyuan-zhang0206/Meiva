# meiva:note
# Replay upper-level checks with fresh policy at each leaf horizon.

import asyncio
from concurrent.futures import Executor

from psycopg_pool import AsyncConnectionPool

from base.agents.history.hierarchy.rebuild import IDLE_WAIT_SECONDS, checks_idle, lift_upper
from base.config import ConfigBoot
from base.db import Database
from services.agent_runner.understanding.group_consumer import (
    MIN_CHECK_OPEN,
    GroupingModels,
    run_group_checks,
)

_IDLE_POLL_SECONDS = 2.0


async def run_rebuild(
    pool: AsyncConnectionPool,
    db: Database,
    models: GroupingModels,
    agent_id: int,
    *,
    config: ConfigBoot,
    executor: Executor | None = None,
    min_check_open: int = MIN_CHECK_OPEN,
) -> int:
    # meiva:note
    # Rebuild the agent's upper levels from its level-1 nodes; the number of leaves replayed.
    #
    # Raises:
    #     TimeoutError: an earlier grouping check still holds its lease after `IDLE_WAIT_SECONDS`.
    waited = 0.0
    while not await checks_idle(pool, agent_id):
        if waited >= IDLE_WAIT_SECONDS:
            raise TimeoutError("a grouping check of the agent is still running")
        await asyncio.sleep(_IDLE_POLL_SECONDS)
        waited += _IDLE_POLL_SECONDS
    leaf_ends = await lift_upper(pool, agent_id)
    for end in leaf_ends:
        await run_group_checks(
            pool,
            db,
            models,
            agent_id,
            config=config,
            executor=executor,
            upto=end,
            min_check_open=min_check_open,
        )
    return len(leaf_ends)
