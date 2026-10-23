# meiva:note
# meiva.ui.show / .close page endpoints + the page reverse proxy.
#
# URLs:
# - `/api/agents/{agent_id}/pages` — list (registry); register / close are the keyed
#   `/api/keyed/v1/agents/{agent_id}/pages` routes.
# - `/pages/{agent_id}-{name}/...` — reverse proxy (one page per agent):
#   the gateway forwards GETs to the agent's page server (host:port from
#   the registry) and returns the content.
# The browser never dials the page server directly — the registered URL is
# the gateway's own, auth-gated by the cluster middleware (session cookie /
# bearer) like every other API route.
#
# The proxy streams the upstream response chunk-by-chunk (StreamingResponse)
# — SSE and arbitrarily large files pass through without being buffered whole.

from __future__ import annotations

import asyncio
import html as _html
import re as _re
from collections.abc import AsyncGenerator
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from psycopg_pool import ConnectionPool
from starlette.background import BackgroundTask

from base.db import agent_exists
from base.events.live.bus import EventBus
from base.events.live.projection import PageClosed, PageOpened
from base.log import logger
from base.packages.docs.pages_copy import (
    PAGE_EXPIRED_BODY,
    PAGE_EXPIRED_TITLE,
    PAGE_LANGUAGE_DEFAULT,
    PAGE_SERVER_DOWN_BODY,
    PAGE_SERVER_TIMEOUT_BODY,
)
from base.telemetry.alerts import display_language
from gateway.http.config import GatewayConfig, runtime_view
from gateway.routers.page_serving import page_acceptance, page_target, page_target_policy
from gateway.schemas import PageCloseRequest, PageRegisterRequest
from ops.pages import (
    get_open_page_target,
    list_all_open_pages,
    list_open_pages,
)
from ops.rpc_schemas import PageRow

router = APIRouter()


# meiva:note
# The page copy language — ``user_settings`` display.language (zh | en).
#
# Same single-language-source mechanism as the IM alert copy
# (``base.telemetry.alerts.display_language``); a missing row or unknown value
# falls back to ``PAGE_LANGUAGE_DEFAULT``. A DB failure also falls back —
# an error page must not turn a proxy 502/504 into a 500 (QA nit A).
def _page_language(pool: ConnectionPool) -> str:
    try:
        with pool.connection() as conn:
            return display_language(conn)
    except Exception:
        logger.opt(exception=True).warning(
            "page copy language lookup failed — falling back to the default",
            event="page_language_lookup_failed",
        )
        return PAGE_LANGUAGE_DEFAULT


# meiva:note
# Small self-contained response for links whose page TTL elapsed.
#
# Copy follows the user's display language (``base/packages/docs/pages_copy.py``, the
# locale module); the page name and agent id pass through untranslated.
# meiva:note
# fixed contract name.
def _EXPIRED_PAGE_HTML(agent_id: int, name: str, lang: str) -> str:  # noqa: N802
    escaped_name = _html.escape(name)
    title = PAGE_EXPIRED_TITLE.get(lang, PAGE_EXPIRED_TITLE[PAGE_LANGUAGE_DEFAULT])
    body = PAGE_EXPIRED_BODY.get(lang, PAGE_EXPIRED_BODY[PAGE_LANGUAGE_DEFAULT])
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><main>"
        f"<h1>{title}</h1>"
        f"<p>{body}</p>"
        f"<p>Page {escaped_name} · agent {agent_id}</p>"
        "</main></body></html>"
    )


# meiva:note
# Response headers forwarded from the page server; everything else
# (hop-by-hop, server internals) is dropped. Content-Length is forwarded too
# (the upstream sets it for static files); an absent one (SSE) makes
# Starlette fall back to chunked transfer.
_FORWARDED_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "cache-control",
        "etag",
        "content-disposition",
        "accept-ranges",
    }
)


# meiva:note
# Render a gateway-relative path as the absolute URL the browser dials.
#
# The base is the configured Gateway URL (`MEIVA_GATEWAY_URL`), NOT the request's own
# Host: the SDK dials the gateway at its local loopback URL on a single box, so
# absolutizing against the request would hand every SDK caller a `localhost` link that
# is unreachable from the user's other devices. The Gateway URL variable is
# the single source of truth for how the gateway is reached from outside —
# whatever the caller's dial address is (localhost / VPN overlay / LAN), the
# delivered URL is the same for everyone.
#
# Falls back to the request base when no Gateway URL is configured (tests /
# unprovisioned checkout) — a gateway that serves pages is expected to be
# configured, so this is a dev-only convenience, not a supported mode.
def _absolute_url(request: Request, path: str) -> str:
    raw_url = request.app.state.gateway_process.config.view.gateway.gateway_url
    base = raw_url.strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return base + path


# meiva:note
# Replace a PageRow's gateway-relative url with the absolute form.
def _absolutize(request: Request, record: PageRow) -> PageRow:
    return record.model_copy(update={"url": _absolute_url(request, record.url)})


# meiva:note
# Reject path segments that could escape the page's served directory.
#
# Defense in depth: the page server (SimpleHTTPRequestHandler) skips `..`
# segments itself, and `name` is already restricted to ^[a-zA-Z0-9_-]+$;
# this keeps a crafted path from even reaching the backend. Rejects
# `.` / `..` segments, backslashes (Windows separators), and control
# characters.
def _validate_proxy_rest(rest: str) -> None:
    for seg in rest.split("/"):
        if seg in (".", "..") or "\\" in seg or any(ord(c) < 0x20 for c in seg):
            raise HTTPException(status_code=400, detail=f"invalid page path segment {seg!r}")


# meiva:note
# Publish a page lifecycle event to the events channel — best-effort, never raises.
async def _publish_page_event(bus: EventBus, event: PageOpened | PageClosed) -> None:
    await bus.publish_best_effort(event.model_dump_json(), context="page_event")


@router.post(
    "/api/keyed/v1/agents/{agent_id}/pages",
    status_code=201,
    response_model=PageRow,
    description="meiva.ui.show / .serve register a page — SDK main entry.\n\nEach agent can have at most one open page. Registering atomically closes any\nexisting open page of the agent (the closed pages get individual PageClosed\nevents so the frontend removes them from the popover) and commits the original\nacceptance, so a replay returns it without a second effect.\n\nReturns 409 when the agent is terminated (dead agents do not show UI), or\nwhen another live page already holds (host, port) — in the port-conflict\ncase the agent's current page is left open and the response detail names\nthe occupying page.",
)
async def post_guarded_page_register(
    agent_id: int,
    body: PageRegisterRequest,
    request: Request,
    key: Annotated[str, Depends(page_acceptance.operation_key)],
) -> PageRow:
    result = await asyncio.to_thread(
        page_acceptance.register,
        request.app.state.db_pool,
        key,
        agent_id,
        body,
        _absolute_url(request, ""),
        request.app.state.page_host_cache,
        runtime_view(request).daemon.page_default_ttl_seconds,
    )
    if not result.replayed:
        for name in result.closed_names:
            await _publish_page_event(
                request.app.state.bus, PageClosed(agent_id=agent_id, name=name)
            )
        await _publish_page_event(
            request.app.state.bus,
            PageOpened(
                agent_id=agent_id,
                page_id=result.record.id,
                name=result.record.name,
                port=result.record.port,
                title=result.record.title,
                url=result.record.url,
            ),
        )
    return result.record


@router.post(
    "/api/keyed/v1/agents/{agent_id}/pages/{name}/close",
    response_model=PageRow,
    description="meiva.ui.close a page — closes only the observed row (`expected_page_id`).\n\nA newer registration under the same name makes the observation stale (409);\nreplay returns the historical acceptance.",
)
async def post_guarded_page_close(
    agent_id: int,
    name: str,
    body: PageCloseRequest,
    request: Request,
    key: Annotated[str, Depends(page_acceptance.operation_key)],
) -> PageRow:
    result = await asyncio.to_thread(
        page_acceptance.close,
        request.app.state.db_pool,
        key,
        agent_id,
        name,
        body.expected_page_id,
        _absolute_url(request, ""),
    )
    if result.closed_names:
        await _publish_page_event(request.app.state.bus, PageClosed(agent_id=agent_id, name=name))
    return result.record


@router.get(
    "/api/pages",
    response_model=list[PageRow],
    description="Fleet view page-load: all agents' currently open pages in one fetch, so\neach fleet row can surface its agent's panels without a per-row request.\nSSE PageOpened/PageClosed handles deltas.",
)
def get_all_pages(request: Request) -> list[PageRow]:
    with request.app.state.db_pool.connection() as conn:
        return [_absolutize(request, r) for r in list_all_open_pages(conn)]


@router.get(
    "/api/agents/{agent_id}/pages",
    response_model=list[PageRow],
    description="UI page-load fetches the agent's currently open page list; SSE PageOpened/Closed handles deltas.",
)
def get_pages(agent_id: int, request: Request) -> list[PageRow]:
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        return [_absolutize(request, r) for r in list_open_pages(conn, agent_id)]


# meiva:note
# Stream the upstream body chunk-by-chunk; an upstream drop mid-body
# ends the stream (the client sees EOF / a truncated body) instead of
# crashing the response.
async def _iter_upstream(resp: httpx.Response) -> AsyncGenerator[bytes, None]:
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    except httpx.HTTPError:
        return


# meiva:note
# Release the upstream connection + client once the streamed response
# has been sent (or the client disconnected).
async def _close_upstream(resp: httpx.Response, client: httpx.AsyncClient) -> None:
    await resp.aclose()
    await client.aclose()


# meiva:note
# Reverse-proxy one GET to the agent's page server, streaming.
#
# The page must be currently open (registry lookup by agent + name). 404
# for unknown/closed page or missing agent; 502 when the page server is
# unreachable; 504 on a stalled transfer. Auth-gated by the cluster
# middleware like every other API route.
#
# Streaming (not buffering): SSE and arbitrarily large files pass through
# chunk-by-chunk — the gateway never holds a whole body in memory.
async def _proxy_page_get_impl(agent_id: int, name: str, rest: str, request: Request) -> Response:
    _validate_proxy_rest(rest)
    target_row = await asyncio.to_thread(
        _page_target_blocking, request.app.state.db_pool, agent_id, name
    )
    if target_row is None:
        expired = await asyncio.to_thread(
            _page_expired_blocking, request.app.state.db_pool, agent_id, name
        )
        if expired:
            lang = await asyncio.to_thread(_page_language, request.app.state.db_pool)
            return Response(
                status_code=410,
                media_type="text/html",
                content=_EXPIRED_PAGE_HTML(agent_id, name, lang),
            )
        raise HTTPException(status_code=404, detail=f"page {name!r} not open (agent {agent_id})")
    host, port = target_row
    await asyncio.to_thread(
        page_target.validate_proxy_page_target,
        request.app.state.db_pool,
        request.app.state.page_host_cache,
        agent_id,
        host,
    )
    target = f"http://{host}:{port}/{rest}"
    query = request.url.query
    if query:
        target += f"?{query}"
    config: GatewayConfig = request.app.state.gateway_config
    dial = page_target_policy.proxy_dial(
        config.page_proxy_connect_seconds, config.page_proxy_read_seconds
    )
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=dial.connect_s, read=dial.read_s, write=dial.write_s, pool=dial.pool_s
        ),
        trust_env=dial.trust_env,
    )
    try:
        req = client.build_request("GET", target)
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException as exc:
        await client.aclose()
        _log_proxy_failure(request, agent_id, name, host, port, "504", exc)
        # meiva:note
        # Friendly, language-aware copy in place of the raw host:port detail —
        # the dial target stays in the log line above (task #2212, QA nit B).
        lang = await asyncio.to_thread(_page_language, request.app.state.db_pool)
        detail = PAGE_SERVER_TIMEOUT_BODY.get(lang, PAGE_SERVER_TIMEOUT_BODY[PAGE_LANGUAGE_DEFAULT])
        raise HTTPException(status_code=504, detail=detail) from None
    except httpx.HTTPError as exc:
        await client.aclose()
        _log_proxy_failure(request, agent_id, name, host, port, "502", exc)
        # meiva:note
        # Friendly, language-aware copy in place of the raw host:port detail —
        # the dial target stays in the log line above (task #2212).
        lang = await asyncio.to_thread(_page_language, request.app.state.db_pool)
        detail = PAGE_SERVER_DOWN_BODY.get(lang, PAGE_SERVER_DOWN_BODY[PAGE_LANGUAGE_DEFAULT])
        raise HTTPException(status_code=502, detail=detail) from None
    headers = {k: v for k, v in resp.headers.items() if k.lower() in _FORWARDED_RESPONSE_HEADERS}
    return StreamingResponse(
        _iter_upstream(resp),
        status_code=resp.status_code,
        headers=headers,
        background=BackgroundTask(_close_upstream, resp, client),
    )


# meiva:note
# One structured log line per failed page-proxy dial (502/504).
#
# Every user-pasted 502 must be traceable end-to-end: the request's
# trace_id (the same value the error envelope returns), the page's
# agent/name, the dial target, and the exception type + message. The
# user-facing response deliberately no longer carries host:port — this
# line is where that detail lives.
def _log_proxy_failure(
    request: Request,
    agent_id: int,
    name: str,
    host: str,
    port: int,
    status: str,
    exc: Exception,
) -> None:
    logger.bind(
        event=f"page_proxy_{status}",
        agent_id=agent_id,
        trace_id=getattr(request.state, "trace_id", None),
        page=name,
        host=host,
        port=port,
        exc_type=type(exc).__name__,
        exc_message=str(exc),
    ).warning(
        "page proxy {status}: page server {host}:{port} failed for agent {agent_id} page {name!r}",
        status=status,
        host=host,
        port=port,
        agent_id=agent_id,
        name=name,
    )


# meiva:note
# --- Page URL: /pages/<agent_id>-<name>/ ---
# One page per agent is the mental model: the URL carries the agent, the
# name is secondary. The composite key is parseable because agent_id is
# numeric — split on the FIRST dash even when the name contains dashes.

_PAGE_KEY_RE = _re.compile(r"^(\d+)-(.*)$")


# meiva:note
# Split `<agent_id>-<name>` on the first dash → (agent_id, name).
#
# None for a malformed key (non-numeric prefix, no dash).
def parse_page_key(page_key: str) -> tuple[int, str] | None:
    m = _PAGE_KEY_RE.match(page_key)
    if m is None:
        return None
    return int(m.group(1)), m.group(2)


@router.get(
    "/pages/{page_key}",
    description="Trailing-slash canonicalization: `/pages/1-foo` redirects to\n`/pages/1-foo/` so relative asset links resolve.",
)
async def proxy_page_root_new(page_key: str) -> Response:
    return RedirectResponse(f"/pages/{page_key}/", status_code=307)


@router.get(
    "/pages/{page_key}/{rest:path}",
    description="Page URL reverse-proxy: `/pages/<agent_id>-<name>/...`.\n\n404 for a malformed key, unknown/closed page, or missing agent.",
)
async def proxy_page_get_new(page_key: str, rest: str, request: Request) -> Response:
    parsed = parse_page_key(page_key)
    if parsed is None:
        raise HTTPException(
            status_code=404, detail=f"invalid page key {page_key!r} (expected <agent_id>-<name>)"
        )
    agent_id, name = parsed
    return await _proxy_page_get_impl(agent_id, name, rest, request)


# meiva:note
# Sync open-page target lookup — via to_thread (the proxy dial path).
def _page_target_blocking(pool: ConnectionPool, agent_id: int, name: str) -> tuple[str, int] | None:
    with pool.connection() as conn:
        return get_open_page_target(conn, agent_id, name)


# meiva:note
# Whether the newest registry row for this page name expired.
def _page_expired_blocking(pool: ConnectionPool, agent_id: int, name: str) -> bool:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT expired_at IS NOT NULL FROM agent_pages "
            "WHERE agent_id = %s AND name = %s ORDER BY id DESC LIMIT 1",
            (agent_id, name),
        )
        row = cur.fetchone()
    return bool(row[0]) if row is not None else False
