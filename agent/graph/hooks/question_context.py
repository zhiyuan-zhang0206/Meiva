# meiva:note
# The agent's open questions for the context it is building, read from the database on the thread the
# graph's event loop does not block. A context without a database (the eval container) or without an
# agent identity has none.
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/ask.md

from __future__ import annotations

import asyncio

from base.agents.context import MeivaContext
from base.agents.questions.model import OpenQuestion
from base.agents.questions.open_questions import read_open_questions


async def read_agent_open_questions(ctx: MeivaContext, agent_id: int | None) -> list[OpenQuestion]:
    if ctx.db is None or agent_id is None:
        return []

    def read() -> list[OpenQuestion]:
        with ctx.require_db().connect() as conn:
            return read_open_questions(conn, agent_id)

    return await asyncio.to_thread(read)
