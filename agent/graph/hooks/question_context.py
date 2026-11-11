# meiva:note
# The agent's open questions for the context it is building, read from the database off the graph's event
# loop. A context without a database (the eval container) or without an agent identity has none. The list
# is shown to the agent and nothing else depends on it, so a database failure is reported and the list is
# left out instead of aborting the turn or the delivery that asked for it.
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/ask.md

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from langchain_core.messages import AnyMessage, BaseMessage

from base.agents.context import MeivaContext
from base.agents.messages.kwargs import read_meiva_kwargs
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


# meiva:note
# A note that lists question ids belongs to the agent whose questions it lists. A fork copies its source's
# messages, so it can hold the source's list; those ids are not the fork's to close, and the note is left
# out of what the fork's model sees. A note that names no owner is shown to every agent.
def mark_questions_owner(message: BaseMessage, agent_id: int) -> BaseMessage:
    message.additional_kwargs["meiva_questions_agent_id"] = agent_id
    return message


def without_foreign_question_notes(
    messages: Sequence[AnyMessage], agent_id: int
) -> list[AnyMessage]:
    return [
        m
        for m in messages
        if read_meiva_kwargs(m).get("meiva_questions_agent_id", agent_id) == agent_id
    ]
