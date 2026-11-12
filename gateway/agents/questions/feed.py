# meiva:note
# The question feed: the unresolved questions, across the fleet or for one agent.
# meiva:link gateway/routers/docs/frontend-ui-data.md

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request
from psycopg.rows import class_row
from psycopg_pool import ConnectionPool

from gateway.agents.schemas import AttentionCounts, QuestionItem, QuestionsFeed
from gateway.http.config import gateway_config, gateway_state

router = APIRouter()

_SELECT = (
    "SELECT n.id, n.agent_id, t.label AS agent_label, n.title, n.content, n.created_at, "
    "n.resolved_at, n.resolution, n.reply, n.question, n.answer, n.source_verified_by "
    "FROM agent_questions n JOIN agents t ON t.id = n.agent_id "
)

_OPEN_FEED_MAX_LIMIT = 500


# meiva:note
# One agent's open set is read whole when no limit is given: a submission must name exactly that set, so
# a cap on it would make the answer impossible. The fleet-wide read is capped by the display setting.
def open_questions(
    pool: ConnectionPool, limit: int | None, *, agent_id: int | None = None
) -> list[QuestionItem]:
    # meiva:link gateway/routers/docs/frontend-ui-data.md
    with pool.connection() as conn, conn.cursor(row_factory=class_row(QuestionItem)) as cur:
        agent_clause = "" if agent_id is None else "AND n.agent_id = %s "
        limit_clause = "" if limit is None else "LIMIT %s"
        params = tuple(v for v in (agent_id, limit) if v is not None)
        cur.execute(
            _SELECT
            + "WHERE n.resolved_at IS NULL "
            + agent_clause
            + "ORDER BY n.id ASC "
            + limit_clause,
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
    if limit is None and agent_id is None:
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


# meiva:note
# Count unread agents once and unresolved question rows in one snapshot.
def attention_counts(pool: ConnectionPool) -> AttentionCounts:
    with pool.connection() as conn, conn.cursor(row_factory=class_row(AttentionCounts)) as cur:
        cur.execute(
            "SELECT (SELECT COUNT(*) FROM agents a WHERE EXISTS ("
            "SELECT 1 FROM agent_publications m WHERE m.agent_id = a.id "
            "AND m.content IS NOT NULL AND m.id > COALESCE(("
            "SELECT seen_id FROM agent_publication_seen WHERE agent_id = a.id), 0))) AS unread_agents, "
            "(SELECT COUNT(*) FROM agent_questions "
            "WHERE resolved_at IS NULL AND question IS NOT NULL) AS open_questions"
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Attention count query returned no row")
        return row


@router.get("/api/attention/counts")
async def get_attention_counts(request: Request) -> AttentionCounts:
    return await asyncio.to_thread(attention_counts, gateway_state(request).db_pool)
