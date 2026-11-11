# meiva:note
# One agent's inbound queue as history — `GET /api/agents/{agent_id}/inbound`.
#
# `GET .../pending` shows only what is still queued, and the timeline shows only what the agent has
# already folded into its conversation. This reads the queue itself, newest first and bounded, so
# "did my message arrive, and has the agent picked it up" has an answer for every status: the row's
# kind, source, status and timestamps, with the content cut to a preview unless the caller asks for
# the whole text. It reads one agent's rows only, through the (agent, created_at) index.

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg import sql
from psycopg.rows import class_row
from pydantic import BaseModel, ConfigDict

from base.agents.messages.inbound import InboundKind
from base.db import agent_exists
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.http.config import gateway_clock, gateway_config, gateway_state

router = APIRouter()

# meiva:note
# The window when the request names none, and the longest one it may name: protective constants
# that bound the rows one read scans.
_DEFAULT_WINDOW_HOURS = 24.0
_MAX_WINDOW_HOURS = 24.0 * 365
# meiva:note
# How much of a message `content` carries unless the caller asks for the whole text.
_PREVIEW_CHARS = 200

InboundStatus = Literal["pending", "claimed", "done"]


# meiva:note
# One inbound row. `content` is the first `_PREVIEW_CHARS` characters (`content_truncated` says it
# was cut) or, with `full=true`, the whole text.
class InboundHistoryItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    kind: InboundKind
    source: str | None
    status: InboundStatus
    created_at: datetime
    claimed_at: datetime | None
    content: str
    content_truncated: bool


# meiva:note
# The effective window and page of one read; `has_more` comes from a one-row lookahead.
class InboundHistoryMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    window_from: datetime
    limit: int
    has_more: bool


class InboundHistoryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: int
    meta: InboundHistoryMeta
    items: list[InboundHistoryItem]


# meiva:note
# The columns of one inbound row as the database holds them.
class _InboundDbRow(BaseModel):
    id: int
    kind: InboundKind
    source: str | None
    status: InboundStatus
    created_at: datetime
    claimed_at: datetime | None
    content: str
    content_truncated: bool


# meiva:note
# The inbound rows of one agent created at or after `since`, newest first, at most `limit + 1` of
# them (the extra row is the lookahead). Each filter is a fixed clause; values travel as parameters.
def _query(
    conn: psycopg.Connection,
    agent_id: int,
    *,
    since: datetime,
    status: InboundStatus | None,
    kind: InboundKind | None,
    source: str | None,
    full: bool,
    limit: int,
) -> list[_InboundDbRow]:
    clauses: list[sql.Composable] = [sql.SQL("agent_id = %s"), sql.SQL("created_at >= %s")]
    params: list[object] = [agent_id, since]
    for column, value in (("status", status), ("kind", kind), ("source", source)):
        if value is not None:
            clauses.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
            params.append(str(value))
    content = (
        sql.SQL("content AS content, false AS content_truncated")
        if full
        else sql.SQL("left(content, %s) AS content, char_length(content) > %s AS content_truncated")
    )
    query = sql.SQL(
        "SELECT id, kind, source, status, created_at, claimed_at, {content} "
        "FROM inbound_messages WHERE {where} ORDER BY created_at DESC, id DESC LIMIT %s"
    ).format(content=content, where=sql.SQL(" AND ").join(clauses))
    preview = [] if full else [_PREVIEW_CHARS, _PREVIEW_CHARS]
    with conn.cursor(row_factory=class_row(_InboundDbRow)) as cur:
        cur.execute(query, [*preview, *params, limit + 1])
        return cur.fetchall()


@router.get(
    "/api/agents/{agent_id}/inbound",
    dependencies=[Depends(deny_isolated_result_read)],
    description=(
        "One agent's inbound queue, newest first: every kind and status, not only what is still "
        "queued (`GET .../pending`) or already in the conversation (the timeline).\n\n"
        "Filters compose (AND): `status` (pending / claimed / done), `kind` (chat, system_note, "
        "heartbeat, ...; an unknown value is 422), `source` (the exact source tag, e.g. `user` or "
        "`agent:12`). The window is the last `hours` (default 24, cap 8760); `limit` defaults to "
        "the configured message window (`display.messages_default_limit`) and is capped at 1000. "
        "`content` is the first 200 characters with `content_truncated` set when cut; "
        "`full=true` returns the whole text. `meta.has_more` says older rows exist inside the "
        "window.\n\n404: the agent does not exist. 503: the read ran past its statement timeout."
    ),
)
# meiva:note
# Read one agent's inbound rows inside the window and filters, newest first, with the statement
# timeout the event reads use; an unknown agent is a 404 before any row is read.
def get_agent_inbound(
    agent_id: int,
    request: Request,
    status: Annotated[InboundStatus | None, Query()] = None,
    kind: Annotated[InboundKind | None, Query()] = None,
    source: Annotated[str | None, Query(min_length=1)] = None,
    hours: Annotated[float | None, Query(gt=0, le=_MAX_WINDOW_HOURS)] = None,
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
    full: Annotated[bool, Query()] = False,  # noqa: FBT002
) -> InboundHistoryResponse:
    now = gateway_clock(request).now()
    since = now - timedelta(hours=hours if hours is not None else _DEFAULT_WINDOW_HOURS)
    shown = limit if limit is not None else gateway_config(request).display.messages_default_limit
    state = gateway_state(request)
    try:
        with state.db_pool.connection() as conn:
            if not agent_exists(conn, agent_id):
                raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(state.gateway_config.events_read_timeout_ms),),
            )
            rows = _query(
                conn,
                agent_id,
                since=since,
                status=status,
                kind=kind,
                source=source,
                full=full,
                limit=shown,
            )
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="inbound read timed out") from exc
    return InboundHistoryResponse(
        agent_id=agent_id,
        meta=InboundHistoryMeta(window_from=since, limit=shown, has_more=len(rows) > shown),
        items=[
            InboundHistoryItem(
                id=row.id,
                kind=row.kind,
                source=row.source,
                status=row.status,
                created_at=row.created_at,
                claimed_at=row.claimed_at,
                content=row.content,
                content_truncated=row.content_truncated,
            )
            for row in rows[:shown]
        ],
    )
