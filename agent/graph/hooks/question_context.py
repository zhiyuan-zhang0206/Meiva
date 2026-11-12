# meiva:note
# The agent's open questions for the context it is building, read from the database off the graph's event
# loop. A context without a database (the eval container) or without an agent identity has none. The list
# is shown to the agent and nothing else depends on it, so a database failure is reported and the list is
# left out instead of aborting the turn or the delivery that asked for it.
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/ask.md

from __future__ import annotations

import asyncio

from base.agents.context import MeivaContext
from base.agents.questions.model import OpenQuestion
from base.agents.questions.open_questions import read_open_questions
from base.log import logger


async def read_agent_open_questions(ctx: MeivaContext, agent_id: int | None) -> list[OpenQuestion]:
    if ctx.db is None or agent_id is None:
        return []

    def read() -> list[OpenQuestion]:
        with ctx.require_db().connect() as conn:
            return read_open_questions(conn, agent_id)

    try:
        return await asyncio.to_thread(read)
    except Exception:
        logger.opt(exception=True).warning(
            "[{label}] could not read agent {agent_id}'s open questions; the list is left out",
            label="open-questions",
            event="open_questions_unavailable",
            agent_id=agent_id,
        )
        return []
