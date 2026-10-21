# meiva:note
# The agent-facing notice surface: post, resolve or withdraw, and the inbox feeds.
# meiva:link gateway/routers/docs/frontend-ui-data.meiva.okf.md

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, LiteralString, cast

import psycopg
from fastapi import APIRouter, Header, HTTPException, Query, Request
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.messages.inbound import InboundKind
from base.agents.messages.inbound_provenance import InboundProvenance
from base.api_contracts.notice_question import ChoiceQuestion
from base.config import settings
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.events.notifications.notices import stage_notice_notification
from base.events.notifications.publish import announce_sync
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.agents.notice_answers import resolution_answer, resolution_inbound
from gateway.agents.schemas import (
    AgentMessageEnqueued,
    NoticeCreateIn,
    NoticeItem,
    NoticesCursor,
    NoticesFeed,
    ResolveNoticeIn,
)
from gateway.http.auth.request_principal import PrincipalScopeError, request_key
from ops import lifecycle as _ops
from ops.agents import get_agent_status

router = APIRouter()
_log = logging.getLogger(__name__)

_SELECT = (
    "SELECT n.id, n.agent_id, t.label, n.title, n.content, n.priority, "
    "n.require_response, n.blocking, n.created_at, n.updated_at, "
    "n.resolved_at, n.resolution, n.reply, n.task_id, n.expire_at, n.question, n.answer "
    "FROM agent_notices n JOIN agents t ON t.id = n.agent_id "
)

# meiva:note
# `_ACTION_RESOLUTION` maps an action to its stored resolution explicitly, never
# inferred from reply presence. The caps bound one response's payload; the
# configurable surface is the *default* window (user ruling 2026-09-17 / #3696).
# meiva:link gateway/routers/docs/frontend-ui-data.meiva.okf.md
_ACTION_RESOLUTION = {"answer": "answered", "dismiss": "dismissed"}
_OPEN_FEED_MAX_LIMIT = 500
_RESOLVED_PAGE_MAX_LIMIT = 100


def publish_response_required_hint(bus: EventBus, agent_id: int) -> None:
    """Refresh the inspector's response-required notice projection after commit."""
    try:
        publish_agent_updated_sync(bus, agent_id)
    except Exception:
        # meiva:note
        # The row is durable; a failed projection refresh must not fail the write.
        # meiva:link gateway/routers/docs/frontend-ui-data.meiva.okf.md
        _log.exception(
            "unable to publish response-required notice hint", extra={"agent_id": agent_id}
        )


def _row_to_item(r: tuple[Any, ...]) -> NoticeItem:
    return NoticeItem(
        id=r[0],
        agent_id=r[1],
        agent_label=r[2],
        title=r[3],
        content=r[4],
        priority=r[5],
        require_response=r[6],
        blocking=r[7],
        created_at=r[8],
        updated_at=r[9],
        resolved_at=r[10],
        resolution=r[11],
        reply=r[12],
        task_id=r[13],
        expire_at=r[14],
        question=r[15],
        answer=r[16],
    )


def _open_notices_blocking(
    pool: ConnectionPool, limit: int, *, agent_id: int | None = None
) -> list[tuple[Any, ...]]:
    # meiva:link gateway/routers/docs/frontend-ui-data.meiva.okf.md
    with pool.connection() as conn, conn.cursor() as cur:
        agent_clause = "" if agent_id is None else "AND n.agent_id = %s "
        params = (limit,) if agent_id is None else (agent_id, limit)
        cur.execute(
            _SELECT
            + "WHERE n.require_response AND n.resolved_at IS NULL "
            + agent_clause
            + "ORDER BY n.priority ASC, n.blocking DESC, n.created_at ASC, n.id ASC LIMIT %s",
            params,
        )
        return cur.fetchall()


def _resolved_notices_blocking(
    pool: ConnectionPool, sql: str, params: list[object]
) -> list[tuple[Any, ...]]:
    """Sync resolved-history query — via to_thread."""
    with pool.connection() as conn, conn.cursor() as cur:
        # The composed query is assembled from fixed literal fragments + typed
        # params only; psycopg's static type demands LiteralString.
        cur.execute(cast(LiteralString, sql), params)
        return cur.fetchall()


@router.get("/api/notices/open")
async def get_open_notices(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=_OPEN_FEED_MAX_LIMIT)] = None,
    agent_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[NoticeItem]:
    # meiva:link gateway/routers/docs/frontend-ui-data.meiva.okf.md
    if limit is None:
        limit = settings.display.notices_open_default_limit
    rows = await asyncio.to_thread(
        _open_notices_blocking,
        request.app.state.db_pool,
        limit,
        agent_id=agent_id,
    )
    return [_row_to_item(r) for r in rows]


def _notices_after_blocking(pool: ConnectionPool, after: int, limit: int) -> list[tuple[Any, ...]]:
    # meiva:link gateway/routers/docs/frontend-ui-data.meiva.okf.md
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            _SELECT
            + "WHERE n.id > %s AND n.resolved_at IS NULL AND n.require_response "
            + "ORDER BY n.id ASC LIMIT %s",
            (after, limit),
        )
        return cur.fetchall()


@router.get("/api/notices/live")
async def get_notices_live(
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int | None, Query(ge=1, le=_OPEN_FEED_MAX_LIMIT)] = None,
) -> list[NoticeItem]:
    # meiva:link gateway/routers/docs/frontend-ui-data.meiva.okf.md
    if limit is None:
        limit = settings.display.notices_open_default_limit
    rows = await asyncio.to_thread(_notices_after_blocking, request.app.state.db_pool, after, limit)
    return [_row_to_item(r) for r in rows]


@router.get("/api/notices")
async def get_notices_feed(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=_OPEN_FEED_MAX_LIMIT)] = None,
    resolved_limit: Annotated[int | None, Query(ge=1, le=_RESOLVED_PAGE_MAX_LIMIT)] = None,
    before_at: Annotated[datetime | None, Query()] = None,
    before_id: Annotated[int | None, Query()] = None,
) -> NoticesFeed:
    # meiva:link gateway/routers/docs/frontend-ui-data.meiva.okf.md
    if (before_at is None) != (before_id is None):
        raise HTTPException(
            status_code=422, detail="before_at and before_id must be supplied together"
        )
    pool = request.app.state.db_pool
    if limit is None:
        limit = settings.display.notices_open_default_limit
    if resolved_limit is None:
        resolved_limit = settings.display.notices_resolved_default_page
    resolved_where = "WHERE n.require_response AND n.resolved_at IS NOT NULL "
    resolved_params: list[object] = []
    if before_at is not None:
        resolved_where += "AND (n.resolved_at, n.id) < (%s, %s) "
        resolved_params.extend([before_at, before_id])
    resolved_params.append(resolved_limit)
    awaiting_rows, resolved_rows = await asyncio.gather(
        asyncio.to_thread(_open_notices_blocking, pool, limit),
        asyncio.to_thread(
            _resolved_notices_blocking,
            pool,
            _SELECT + resolved_where + "ORDER BY n.resolved_at DESC, n.id DESC LIMIT %s",
            resolved_params,
        ),
    )
    resolved_page = [_row_to_item(r) for r in resolved_rows]
    next_cursor = None
    if len(resolved_page) == resolved_limit:
        last = resolved_page[-1]
        if last.resolved_at is not None:
            next_cursor = NoticesCursor(before_at=last.resolved_at, before_id=last.id)
    return NoticesFeed(
        open=[],
        awaiting=[_row_to_item(r) for r in awaiting_rows],
        resolved_page=resolved_page,
        next_cursor=next_cursor,
    )


async def post_notice_resolve(
    agent_id: int,
    notice_id: int,
    body: ResolveNoticeIn,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> AgentMessageEnqueued:
    # meiva:note
    # Resolution and reply delivery share one durable receipt. A dismissal is a
    # notice-system event, never user authorization or ordinary user speech.
    # meiva:link agent/messages/docs/message-format.meiva.okf.md
    idempotency_key = notice_key(request, idempotency_key)
    action = body.action
    reply = body.reply

    resolution: _ops.NoticeResolutionFacts | None = None

    def _resolve(conn: psycopg.Connection) -> str | None:
        nonlocal resolution
        if action == "read":
            raise HTTPException(
                status_code=422, detail="notices require a reply or an explicit dismissal"
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, require_response, resolved_at, question, expire_at, created_at "
                "FROM agent_notices WHERE id = %s AND agent_id = %s FOR UPDATE",
                (notice_id, agent_id),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"notice {notice_id} does not exist for agent {agent_id}",
                )
            title, require_response, resolved_at, raw_question, expire_at, created_at = row
            if raw_question is not None and expire_at <= datetime.now(UTC):
                raise HTTPException(status_code=409, detail="this question has expired")
            resolved_answer = resolution_answer(body, raw_question)
            reply = resolved_answer.reply
            resolution = _ops.notice_resolution_facts(
                _ACTION_RESOLUTION[action],
                require_response=bool(require_response),
                created_at=created_at,
                replied=reply is not None or resolved_answer.answer is not None,
            )
            if resolved_at is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"notice {notice_id} is already resolved for agent {agent_id}",
                )
            if not require_response:
                raise HTTPException(status_code=409, detail="legacy FYI entries are not notices")
            if action == "answer" and reply is None:
                raise HTTPException(status_code=422, detail="action 'answer' requires a reply")
            cur.execute(
                "UPDATE agent_notices SET resolved_at = now(), resolution = %s, reply = %s, answer = %s "
                "WHERE id = %s",
                (
                    _ACTION_RESOLUTION[action],
                    reply,
                    Jsonb(resolved_answer.answer.model_dump(mode="json"))
                    if resolved_answer.answer is not None
                    else None,
                    notice_id,
                ),
            )
        return resolution_inbound(title, action, resolved_answer.reply)

    # meiva:note
    # Every notice resolution rides the notice-system source (reply / dismiss); the claim node
    # wraps both as a system note, so no User-role message is consumed.
    # meiva:link agent/messages/docs/message-format.meiva.okf.md
    deliver_source = (
        "system:notice-reply"
        if reply is not None or body.answer is not None
        else "system:notice-dismiss"
    )
    receipt = await asyncio.to_thread(
        resolve_once,
        request.app.state.db_pool,
        request.url.path,
        idempotency_key,
        body.model_dump(mode="json", exclude={"answer"} if body.answer is None else set()),
        agent_id,
        deliver_source,
        _resolve,
        request_inbound_provenance(request),
    )
    inbound_id = receipt["inbound_id"]
    if inbound_id is not None:
        if not isinstance(inbound_id, int):
            raise TypeError("notice inbound receipt must be an integer")
        await asyncio.to_thread(
            publish_inbound_wake,
            request.app.state.db,
            request.app.state.bus,
            agent_id,
            str(inbound_id),
        )
        content = receipt["content"]
        if not isinstance(content, str):
            raise TypeError("notice inbound content must be a string")
        await _ops.publish_inbound_arrived(
            request.app.state.bus,
            agent_id,
            inbound_id,
            "chat",
            deliver_source,
            content,
        )
        status = await _ops.resurrect_if_terminated(
            request.app.state.db,
            request.app.state.bus,
            agent_id,
            trigger_inbound_id=inbound_id,
            trigger_inbound_kind=InboundKind.CHAT,
            local_machine=request.app.state.machine.name(),
        )
    else:
        status = await asyncio.to_thread(get_agent_status, request.app.state.db, agent_id)
    await _ops.publish_notice_resolved(request.app.state.bus, agent_id, notice_id, facts=resolution)
    return AgentMessageEnqueued(status=status, inbound_id=inbound_id)


# ── unified notice write API (R3 door ④) ─────────────────────────────────
# meiva:note
# The agent->user queue has ONE write path: the agent SDK calls these endpoints
# (meiva.ui.notify here; edit_notice / dismiss_notice through the guarded routes
# in `notice_operations`), which perform the whole lifecycle atomically in one
# transaction — a one-open-notice check plus insert for notify, an observed-row
# update for edit/dismiss — plus the event publishes. The agent_notices table is
# the single truth source and every writer goes through the same code.
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/notify.meiva.okf.md
#
# Pause semantics: data-plane like every other agent write — the quiesce has
# drained the agents before the migration window, so nothing legitimately
# writes notices during pause.


@router.post("/api/agents/{agent_id}/notices", status_code=201)
async def post_notice_create(
    agent_id: int,
    body: NoticeCreateIn,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> dict:
    # meiva:link meiva_builtins/plugins/meiva_fleet/docs/notify.meiva.okf.md
    idempotency_key = notice_key(request, idempotency_key)
    request_body: dict[str, object] = body.model_dump(
        mode="json", exclude={"question"} if body.question is None else set()
    )

    def _create(
        pool: ConnectionPool,
    ) -> tuple[
        int,
        int,
        list[dict[str, object]],
        bool,
        int | None,
    ]:
        with write_transaction(pool) as conn, conn.cursor() as cur:
            previous = existing_receipt(
                conn,
                request.url.path,
                idempotency_key,
                request_body,
                legacy_body=(request_body | {"require_response": False})
                if "require_response" not in body.model_fields_set
                else None,
            )
            if previous is not None:
                return (
                    int(cast(int, previous["global_id"])),
                    int(cast(int, previous["id"])),
                    cast(list[dict[str, object]], previous["pending_notices"]),
                    True,
                    cast(int | None, previous.get("notification_id")),
                )
            validate_creation(body)
            validate_creation_state(conn, body)
            # Serialize all creations for an agent, including different keys:
            # no-open-notice races must not allocate the same local id.
            cur.execute("SELECT id FROM agents WHERE id = %s FOR UPDATE", (agent_id,))
            # meiva:note
            # One open notice per agent, and no silent replacement: a second
            # notify() is refused with the open notice's id and the two ways
            # out, instead of resolving the first one on the writer's behalf.
            # Legacy FYI rows (require_response false, unwritable since
            # 2026-10-19) are not notices and never block a poster.
            # meiva:link meiva_builtins/plugins/meiva_fleet/docs/notify.meiva.okf.md
            cur.execute(
                "SELECT id, local_id, title FROM agent_notices "
                "WHERE agent_id = %s AND resolved_at IS NULL AND require_response "
                "ORDER BY created_at DESC, local_id DESC LIMIT 1",
                (agent_id,),
            )
            open_notice = cur.fetchone()
            if open_notice is not None:
                open_local_id = int(open_notice[1])
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"agent {agent_id} already holds an open notice "
                        f"#{open_local_id} ({open_notice[2]!r}); an agent holds one "
                        f"notice at a time. Close it first with meiva.ui.dismiss_notice() "
                        f"(or answer it in the console), or rewrite it in place with "
                        f"meiva.ui.edit_notice(...), then post the new one."
                    ),
                )
            now = datetime.now(UTC)
            max_expire = now + timedelta(seconds=settings.daemon.notice_ttl_limit_seconds)
            if body.expire_at is None:
                final_expire_at = max_expire
            else:
                req_expire = body.expire_at
                if req_expire.tzinfo is None:
                    req_expire = req_expire.replace(tzinfo=UTC)
                final_expire_at = min(req_expire, max_expire)

            # Insert the new notice with the agent's next local id.
            cur.execute(
                "INSERT INTO agent_notices "
                "(agent_id, local_id, title, content, priority, require_response, blocking, task_id, expire_at, question) "
                "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices "
                "WHERE agent_id = %s), -1) + 1, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, local_id",
                (
                    agent_id,
                    agent_id,
                    body.title,
                    body.content,
                    body.priority,
                    body.require_response,
                    body.blocking,
                    body.task_id,
                    final_expire_at,
                    Jsonb(body.question.model_dump(mode="json"))
                    if body.question is not None
                    else None,
                ),
            )
            row = cur.fetchone()
            if row is None:  # pragma: no cover — RETURNING always yields
                raise RuntimeError("notice insert returned no row")
            notice_global_id, notice_local_id = int(row[0]), int(row[1])
            notification_id = stage_notice_notification(
                conn, agent_id=agent_id, notice_id=notice_global_id
            )
            cur.execute(
                "SELECT local_id AS id, title, created_at, priority "
                "FROM agent_notices WHERE agent_id = %s AND resolved_at IS NULL "
                "ORDER BY created_at DESC, local_id DESC",
                (agent_id,),
            )
            pending: list[dict[str, object]] = [
                {"id": int(r[0]), "title": r[1], "created_at": r[2].isoformat(), "priority": r[3]}
                for r in cur.fetchall()
            ]
            save_receipt(
                conn,
                request.url.path,
                idempotency_key,
                request_body,
                {
                    "global_id": notice_global_id,
                    "id": notice_local_id,
                    "pending_notices": pending,
                    "notification_id": notification_id,
                },
            )
            # Publish only after the durable rows are visible to the snapshot
            # query; a pre-commit AgentUpdated would preserve the stale view.
            conn.commit()
            if body.require_response:
                publish_response_required_hint(request.app.state.bus, agent_id)
            return (
                notice_global_id,
                notice_local_id,
                pending,
                False,
                notification_id,
            )

    pool = request.app.state.db_pool
    (
        notice_global_id,
        notice_local_id,
        pending,
        replayed,
        notification_id,
    ) = await asyncio.to_thread(_create, pool)
    # meiva:note
    # The posted event publishes after the commit, by GLOBAL id; the SDK's return
    # value carries LOCAL ids. Best-effort, never raises.
    # meiva:link gateway/agents/docs/notice-receipts.meiva.okf.md
    if not replayed:
        await _ops.publish_notice_posted(
            request.app.state.bus,
            agent_id,
            notice_global_id,
            body.priority,
            body.title,
            body.task_id,
        )
    else:
        await asyncio.to_thread(publish_response_required_hint, request.app.state.bus, agent_id)
    await announce_notice_delivery(request, notification_id)
    return {
        "id": int(notice_local_id),
        "pending_count": len(pending),
        "pending_notices": pending,
    }


# ── keyed notice-operation receipts (owned by the notice transaction, never a response cache) ──


def notice_key(request: Request, key: str) -> str:
    """Use the same credential scope as other gateway keyed operations."""
    try:
        return request_key(request, key, method="POST", path=request.url.path)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# meiva:note
# `announce_sync` publishes only an unannounced row, so a replay repairs a lost
# announce without pushing one that already landed.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
async def announce_notice_delivery(request: Request, notification_id: int | None) -> None:
    if notification_id is None:
        return
    await asyncio.to_thread(
        announce_sync, request.app.state.db_pool, request.app.state.bus, notification_id
    )


def existing_receipt(
    conn: psycopg.Connection,
    path: str,
    key: str,
    body: dict[str, object],
    *,
    legacy_body: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """Serialize a notice operation and reject reuse with changed inputs."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (path + ":" + key,))
        cur.execute(
            "SELECT request, receipt FROM notice_operation_receipts "
            "WHERE path = %s AND operation_key = %s",
            (path, key),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if row[0] != body and row[0] != legacy_body:
        raise HTTPException(
            status_code=409, detail="idempotency key identifies a different notice operation"
        )
    return row[1]


def save_receipt(
    conn: psycopg.Connection,
    path: str,
    key: str,
    body: dict[str, object],
    receipt: dict[str, object],
) -> None:
    """Persist an immutable result in the transaction containing its effects."""
    conn.execute(
        "INSERT INTO notice_operation_receipts (path, operation_key, request, receipt) "
        "VALUES (%s, %s, %s, %s)",
        (path, key, Jsonb(body), Jsonb(receipt)),
    )


def resolve_once(
    pool: ConnectionPool,
    path: str,
    key: str,
    body: dict[str, object],
    agent_id: int,
    source: str,
    prepare: Callable[[psycopg.Connection], str | None],
    provenance: InboundProvenance,
) -> dict[str, object]:
    """Resolve and insert the reply with one durable operation receipt."""
    with write_transaction(pool) as conn:
        previous = existing_receipt(conn, path, key, body)
        if previous is not None:
            return previous
        content = prepare(conn)
        inbound_id = None
        if content is not None:
            with conn.cursor() as cur:
                inbound_id, _ = insert_inbound_message_in_transaction(
                    cur,
                    agent_id,
                    content,
                    source,
                    provenance=provenance,
                )
        receipt: dict[str, object] = {"inbound_id": inbound_id, "content": content}
        save_receipt(conn, path, key, body, receipt)
    return receipt


def validate_creation(body: NoticeCreateIn) -> None:
    """Reject invalid notice obligations before allocating a receipt."""
    if not body.title.strip():
        raise HTTPException(status_code=422, detail="title must be non-empty")
    if not body.require_response:
        raise HTTPException(
            status_code=422,
            detail="notices require a user response; send ordinary information in chat",
        )

    if isinstance(body.question, ChoiceQuestion) and body.question.allow_other:
        raise HTTPException(
            status_code=422,
            detail="Other is not an answer option; use a choice with an optional comment",
        )


def validate_creation_state(conn: psycopg.Connection, body: NoticeCreateIn) -> None:
    """Validate mutable creation preconditions only for a fresh operation."""
    if body.task_id is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM agent_tasks WHERE id = %s", (body.task_id,))
            if cur.fetchone() is None:
                raise HTTPException(status_code=422, detail=f"task {body.task_id} does not exist")
    if body.expire_at is not None:
        expiry = (
            body.expire_at
            if body.expire_at.tzinfo is not None
            else body.expire_at.replace(tzinfo=UTC)
        )
        if expiry < datetime.now(UTC):
            raise HTTPException(
                status_code=422, detail=f"expire_at is in the past: {expiry.isoformat()}"
            )
