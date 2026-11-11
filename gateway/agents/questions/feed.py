# meiva:note
# The question feed: the unresolved questions, across the fleet or for one agent.
# meiva:link gateway/routers/docs/frontend-ui-data.md

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request
from psycopg.rows import class_row
from psycopg_pool import ConnectionPool

from gateway.agents.schemas import QuestionItem, QuestionsFeed
from gateway.http.config import gateway_config, gateway_state

router = APIRouter()

_SELECT = (
    "SELECT n.id, n.agent_id, t.label AS agent_label, n.title, n.content, n.created_at, "
    "n.resolved_at, n.resolution, n.reply, n.question, n.answer, n.source_verified_by "
    "FROM agent_questions n JOIN agents t ON t.id = n.agent_id "
)

_OPEN_FEED_MAX_LIMIT = 500


def open_questions(
    pool: ConnectionPool, limit: int, *, agent_id: int | None = None
) -> list[QuestionItem]:
    # meiva:link gateway/routers/docs/frontend-ui-data.md
    with pool.connection() as conn, conn.cursor(row_factory=class_row(QuestionItem)) as cur:
        agent_clause = "" if agent_id is None else "AND n.agent_id = %s "
        params = (limit,) if agent_id is None else (agent_id, limit)
        cur.execute(
            _SELECT + "WHERE n.resolved_at IS NULL " + agent_clause + "ORDER BY n.id ASC LIMIT %s",
            params,
        )
        return cur.fetchall()


@router.get("/api/questions/open")
async def get_open_questions(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=_OPEN_FEED_MAX_LIMIT)] = None,
    agent_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[QuestionItem]:
    # meiva:link gateway/routers/docs/frontend-ui-data.md
    if limit is None:
        limit = gateway_config(request).display.questions_open_default_limit
    return await asyncio.to_thread(
        open_questions,
        gateway_state(request).db_pool,
        limit,
        agent_id=agent_id,
    )


@router.get("/api/questions")
async def get_questions_feed(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=_OPEN_FEED_MAX_LIMIT)] = None,
) -> QuestionsFeed:
    # meiva:link gateway/routers/docs/frontend-ui-data.md
    if limit is None:
        limit = gateway_config(request).display.questions_open_default_limit
    awaiting = await asyncio.to_thread(open_questions, gateway_state(request).db_pool, limit)
    return QuestionsFeed(awaiting=awaiting)
