# meiva:note
# Screen a wake for a turn: the agent's stored row, and whether this host runs it.
# Read once per turn, before admission (`AgentHost._run_turn`): the row carries the
# machine, status, harness and two config maps the turn needs, and the quiet
# rejections decide whether this host is the one to run it. Split out of `host.py`
# to keep it inside the file-size ceiling.
# meiva:link base/agents/docs/harness.meiva.okf.md

from __future__ import annotations

from collections.abc import Collection

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.agents.birth.harness import LAUNCHABLE_HARNESSES
from base.api_contracts.agent_contract import Harness
from base.log import logger
from services.agent_runner.agent_host.runtime import StoredConfig

__all__ = ["is_runnable", "read_stored_config"]


# meiva:note
# Whether the host at `machine` should hand `agent_id` a turn right now. The
# rejections stay quiet rather than WARNING: a foreign agent's wake is normal
# cross-talk (the dispatcher's pattern subscription is cluster-wide, so every
# runner sees every wake), a terminated agent's wake is the delivery watchdog's
# resurrect path, and a row born on a harness this release has no launcher for
# has nothing this host could run it on. None is a fault of this host.
# meiva:link base/agents/docs/harness.meiva.okf.md
def is_runnable(
    machine: str,
    agent_id: int,
    stored: StoredConfig,
    *,
    launchable: Collection[Harness] = LAUNCHABLE_HARNESSES,
) -> bool:
    if stored.machine != machine:
        logger.debug(
            "hosted wake for agent {agent_id} belongs to machine {owner} — not ours",
            agent_id=agent_id,
            owner=stored.machine,
        )
        return False
    if stored.status == "terminated":
        logger.info(
            "hosted wake for agent {agent_id} ignored — status {status} is not runnable",
            agent_id=agent_id,
            status=stored.status,
        )
        return False
    # meiva:note
    # Every hosted turn enters through here, and the launch op is only one way
    # into a turn: the delivery watchdog, the durable pending scan and pub/sub
    # wakes reach it without one. Without a launcher the only loop this host
    # could run for a row is the built-in one, which is not what the row says.
    # meiva:link base/agents/docs/harness.meiva.okf.md
    if Harness(stored.harness) not in launchable:
        logger.debug(
            "hosted wake for agent {agent_id} ignored — harness {harness} has no launcher",
            agent_id=agent_id,
            harness=stored.harness,
        )
        return False
    return True


# meiva:note
# One round trip over the row a turn needs: where it lives (machine), whether it
# may run (status), which runtime loop it was born on (harness) and the two config
# maps that decide what running means. None = the row is gone, a real anomaly (a
# wake was published for an agent that does not exist), and is logged rather than
# screened like the rejections above.
# meiva:link base/agents/docs/harness.meiva.okf.md
async def read_stored_config(
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection], agent_id: int
) -> StoredConfig | None:
    async with control_pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT machine, status, harness, config_overlay, birth_config "
                "FROM agents_meta WHERE id = %s",
                (agent_id,),
            )
        ).fetchone()
    if row is None:
        logger.warning(
            "hosted wake for agent {agent_id} has no agents_meta row — ignoring",
            agent_id=agent_id,
        )
        return None
    return StoredConfig(
        machine=row[0],
        status=row[1],
        harness=row[2],
        config_overlay=row[3],
        birth_config=row[4],
    )
