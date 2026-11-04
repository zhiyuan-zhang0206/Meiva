"""Placement and termination boundaries for a queued resurrection."""

from dataclasses import dataclass

import psycopg

from base.api_contracts.agent_contract import (
    AgentNotFound,
    MachinePaused,
    ResurrectError,
)
from base.db.rows import query_one
from base.log import logger


class ResurrectTriggerStaleError(ResurrectError):
    """The exact pending wake no longer qualifies; the local op returns a no-op."""


# meiva:note
# The placement column of an `agents_meta` row, which a queued resurrection is bound to.
@dataclass(frozen=True)
class _Placement:
    machine: str


def lock_active_home_machine(cur: psycopg.Cursor, agent_id: int) -> str:
    """Share the pause latch before metadata/inbound locks or budget writes."""
    agent_row = query_one(
        cur.connection, _Placement, "SELECT machine FROM agents_meta WHERE id = %s", (agent_id,)
    )
    if agent_row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    home_machine = agent_row.machine
    cur.execute("SELECT paused_at FROM machines WHERE name = %s FOR SHARE", (home_machine,))
    machine_row = cur.fetchone()
    if machine_row is not None and machine_row[0] is not None:
        raise MachinePaused(
            f"agent {agent_id} home machine {home_machine!r} is paused; "
            "resume it before resurrecting"
        )
    return home_machine


# meiva:note
# Report a swallowed auto-resurrect failure; the triggering inbound stays queued.
#
# The failure may be transient, so it is INFO; a manual resurrect picks the
# queued inbound up.
def report_auto_resurrect_failure(agent_id: int, exc: Exception) -> None:
    logger.opt(exception=exc).info(
        "auto-resurrect of agent {agent_id} failed; inbound queued, manual resurrect "
        "will pick it up",
        event="auto_resurrect_failed",
        agent_id=agent_id,
    )
