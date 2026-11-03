# meiva:note
# Server-side browser-session authentication and revocation endpoints.
#
# POST /api/auth/login               — verify the web login password, set session cookie
# PUT  /api/auth/password            — set the web login password (a human credential only)
# POST /api/auth/login-ticket        — issue a one-time, short-lived login ticket
# POST /api/auth/login-ticket/redeem — exchange a ticket for a session cookie
# POST /api/auth/logout              — revoke and clear the current session cookie
# GET  /api/auth/check               — report whether the current session is valid

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from base.cluster.rate_limit import LoginRateLimiter
from base.host.db_authority.api import AcceptanceCache
from base.host.net.auth import (
    clear_cookie_header,
    cookie_name,
    is_managed_browser_user_agent,
    session_cookie_header,
)
from gateway.http.auth.login_password import password_problem
from gateway.http.auth.login_password_store import LoginPasswords
from gateway.http.auth.rejection_log import AuthRejectionLog
from gateway.http.auth.request_principal import (
    SessionKeys,
    current_session_fact,
    human_mint,
    login_mint,
    require_human_credential,
    session_mints,
)
from gateway.http.auth.session_store import (
    LOGIN_TICKET_TTL_SECONDS,
    SessionStore,
    SessionTouchThrottle,
    WebSessionRow,
    list_sessions,
    minted_session_id,
    minted_ticket_id,
    session_ids_with_suffix,
    session_mint,
    ticket_mint,
)
from gateway.http.config import (
    GatewayAppState,
    GatewayConfig,
    gateway_clock,
    gateway_state,
    state_as,
)
from gateway.http.middleware.cors import request_host, session_cookie_secure
from gateway.http.middleware.error_envelope import error_response

router = APIRouter()


# meiva:note
# The session stores and caches the app root builds; the auth routes and the auth middleware
# (`gateway/app.py`) are their readers, so this module declares them.
class AuthAppState(GatewayAppState):
    sessions: SessionStore
    session_keys: SessionKeys
    login_passwords: LoginPasswords
    session_touch: SessionTouchThrottle
    auth401_log: AuthRejectionLog


# meiva:note
# POST /api/auth/login body. `password` is the web login password (or, for the
# managed browser of an agent-runner, the active generation's runner API
# token); a missing or empty one falls through to the 401 below rather than a
# 422. `username` is accepted for Chrome password-manager compatibility but
# never validated.
class LoginRequest(BaseModel):
    password: str = ""
    username: str | None = None


# meiva:note
# PUT /api/auth/password body: the new web login password in clear text, which the
# gateway stores only as a salted hash.
class SetPasswordRequest(BaseModel):
    password: str


class LoginTicketRedeemRequest(BaseModel):
    """POST /api/auth/login-ticket/redeem body; a missing or empty ticket falls
    through to the 401 rather than a 422, like a missing password."""

    ticket: str = ""


# meiva:note
# Authenticate with the web login password and receive a session cookie.
#
# Request body: ``{"password": "<web-login-password>"}``. An agent-runner's
# managed browser presents the active generation's runner API token instead.
# The cluster secret is not a login password, and a gateway whose password was
# never set (``PUT /api/auth/password``) refuses every password: its browser
# sessions open from a login ticket. The session is bound to the credential
# that minted it (`login_mint`): it stops authenticating when that generation
# is revoked or the password is set again, whatever its remaining lifetime.
#
# On success, returns ``{"ok": true}`` and sets an HTTP-only session
# cookie whose lifetime is controlled by ``session_ttl_seconds``.
#
# On failure, returns a typed 401 error envelope with detail ``"invalid password"``.
#
# Brute-force guard: an IP that fails ``gateway.login_max_failures`` times in
# a row is locked for ``gateway.login_lockout_seconds`` (policy + rationale on
# those config fields; enforcement in base/cluster/rate_limit.py).
# While locked, the endpoint returns 429 + ``Retry-After`` instead of 401 —
# 401 would read as "wrong password" and invite exactly the retry loop the
# lockout exists to stop. A successful login resets the IP's counter.
@router.post("/api/auth/login")
async def login(body: LoginRequest, request: Request) -> JSONResponse:
    ip = request.client.host if request.client else "unknown"

    login_limiter: LoginRateLimiter = gateway_state(request).login_limiter
    remaining = login_limiter.lockout_remaining(ip, now=gateway_clock(request).now().timestamp())
    if remaining > 0:
        return error_response(
            request,
            code="login_rate_limited",
            status=429,
            detail="too many failed login attempts",
            retryable=True,
            extensions={"retry_after_seconds": remaining},
            headers={"Retry-After": str(remaining)},
        )

    password = body.password

    if not password:
        login_limiter.record_failure(ip, now=gateway_clock(request).now().timestamp())
        return error_response(
            request,
            code="invalid_password",
            status=401,
            detail="invalid password",
            retryable=False,
        )

    mint = await asyncio.to_thread(
        login_mint,
        state_as(request, AuthAppState).session_keys,
        state_as(request, AuthAppState).login_passwords,
        password,
        home=gateway_state(request).gateway_process.home,
        cache=gateway_state(request).machine_token_acceptance,
    )
    if mint is None:
        login_limiter.record_failure(ip, now=gateway_clock(request).now().timestamp())
        return error_response(
            request,
            code="invalid_password",
            status=401,
            detail="invalid password",
            retryable=False,
        )

    login_limiter.record_success(ip)
    return await _open_session(request, mint, ip)


async def _open_session(request: Request, mint: str, ip: str) -> JSONResponse:
    """Create a session bound to `mint` and answer with its cookie."""
    session_id = minted_session_id(mint)
    config: GatewayConfig = gateway_state(request).gateway_config
    ttl_seconds = config.session_ttl_seconds
    await asyncio.to_thread(
        state_as(request, AuthAppState).sessions.create,
        session_id,
        ttl_seconds,
        request.headers.get("user-agent", ""),
        ip,
    )
    headers = session_cookie_header(
        session_id,
        secure=session_cookie_secure(
            str(request.url),
            browser_origins=config.browser_origins,
            default_secure=config.session_cookie_secure,
        ),
        ttl_seconds=ttl_seconds,
    )
    return JSONResponse(content={"ok": True}, headers=headers)


# meiva:note
# Set the web login password, replacing the one before it.
#
# Request body: ``{"password": "<new-password>"}``, at least
# ``MIN_PASSWORD_LENGTH`` characters (else a typed 422 ``password_too_short``). Only a human
# credential may set it (the cluster secret, a human login session or a trusted network); a
# write generation's machine token gets 403, so no machine credential chooses what a browser
# logs in with. The gateway stores a salted scrypt hash in
# ``$MEIVA_HOME/web-login-password.json`` and never the password itself.
#
# Setting a password ends every session the previous one minted, the caller's own included
# when it logged in with that password, even when the new password equals the old one (the
# stored bytes carry a fresh salt). Sessions a login ticket opened and a unit's managed
# browser sessions stay. Returns ``{"ok": true}``.
@router.put("/api/auth/password", dependencies=[Depends(require_human_credential)])
async def set_login_password(body: SetPasswordRequest, request: Request) -> JSONResponse:
    problem = password_problem(body.password)
    if problem is not None:
        return error_response(
            request, code="password_too_short", status=422, detail=problem, retryable=False
        )
    await asyncio.to_thread(
        state_as(request, AuthAppState).login_passwords.set,
        gateway_state(request).gateway_process.home.resolve(),
        body.password,
    )
    return JSONResponse(content={"ok": True})


# meiva:note
# Issue a one-time login ticket for a browser, from a human caller.
#
# The ticket is a `web_sessions` row valid for ``LOGIN_TICKET_TTL_SECONDS``
# and bound to the cluster secret's mint (`human_mint`), not to the web login
# password: redeeming it (``/api/auth/login-ticket/redeem``) opens a session
# that outlives a password set again and dies when the secret rotates, so a
# gateway with no password set still has a way in. Only a human credential
# (the secret, a human login session or a trusted network) may issue one; a
# write generation's machine token gets 403, so no machine credential can
# mint a browser session.
#
# Returns ``{"ticket": "<opaque>", "expires_in_seconds": <int>}``.
@router.post("/api/auth/login-ticket", dependencies=[Depends(require_human_credential)])
async def issue_login_ticket(request: Request) -> JSONResponse:
    config: GatewayConfig = gateway_state(request).gateway_config
    mint = await asyncio.to_thread(
        human_mint,
        state_as(request, AuthAppState).session_keys,
        config.cluster_secret,
        home=gateway_state(request).gateway_process.home,
    )
    ticket = minted_ticket_id(mint)
    ip = request.client.host if request.client else "unknown"
    await asyncio.to_thread(
        state_as(request, AuthAppState).sessions.create,
        ticket,
        LOGIN_TICKET_TTL_SECONDS,
        "login-ticket",
        ip,
    )
    return JSONResponse(
        content={"ticket": ticket, "expires_in_seconds": LOGIN_TICKET_TTL_SECONDS},
        headers={"Cache-Control": "no-store"},
    )


async def _consume_login_ticket(request: Request, ticket: str) -> str | None:
    """Spend `ticket` and return its mint, or None when it cannot open a session.

    The revoke is the single-use gate (atomic: it matches only an unrevoked,
    unexpired row); a spent ticket whose mint is no longer admitted (the secret
    rotated, a generation was revoked) is still spent.
    """
    mint = ticket_mint(ticket)
    if mint is None or not await asyncio.to_thread(
        state_as(request, AuthAppState).sessions.revoke, ticket
    ):
        return None
    config: GatewayConfig = gateway_state(request).gateway_config
    admitted = await asyncio.to_thread(
        session_mints,
        state_as(request, AuthAppState).session_keys,
        state_as(request, AuthAppState).login_passwords,
        config.cluster_secret,
        home=gateway_state(request).gateway_process.home,
        cache=gateway_state(request).machine_token_acceptance,
    )
    return mint if mint in admitted else None


@router.post("/api/auth/login-ticket/redeem")
async def redeem_login_ticket(body: LoginTicketRedeemRequest, request: Request) -> JSONResponse:
    """Exchange a login ticket for a session cookie; the ticket is consumed.

    Needs no credential: the ticket is the credential. Redemption revokes the
    ticket row in one atomic statement, so a ticket opens at most one session,
    and only while it is unexpired and the credential that issued it is still
    current. An unknown, malformed, replayed, expired or orphaned ticket all
    get the same 401, and every failure counts against the login limiter
    (429 while locked), so tickets cannot be guessed at.
    """
    ip = request.client.host if request.client else "unknown"
    login_limiter: LoginRateLimiter = gateway_state(request).login_limiter
    remaining = login_limiter.lockout_remaining(ip, now=gateway_clock(request).now().timestamp())
    if remaining > 0:
        return error_response(
            request,
            code="login_rate_limited",
            status=429,
            detail="too many failed login attempts",
            retryable=True,
            extensions={"retry_after_seconds": remaining},
            headers={"Retry-After": str(remaining)},
        )

    mint = await _consume_login_ticket(request, body.ticket)
    if mint is None:
        login_limiter.record_failure(ip, now=gateway_clock(request).now().timestamp())
        return error_response(
            request,
            code="invalid_login_ticket",
            status=401,
            detail="invalid or expired login ticket",
            retryable=False,
        )

    login_limiter.record_success(ip)
    return await _open_session(request, mint, ip)


@router.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    """Revoke and clear the current session cookie; repeated calls are safe."""
    session_id = request.cookies.get(cookie_name())
    if session_id:
        await asyncio.to_thread(state_as(request, AuthAppState).sessions.revoke, session_id)
    return JSONResponse(
        content={"ok": True},
        headers=clear_cookie_header(),
    )


@router.get("/api/auth/check")
async def check(request: Request) -> JSONResponse:
    """Report whether the request carries a valid session cookie.

    Returns ``{"authenticated": true}`` or ``{"authenticated": false}``.
    Always 200 — the caller uses the body, not the status code.
    """
    token = request.cookies.get(cookie_name())
    config: GatewayConfig = gateway_state(request).gateway_config
    fact = await asyncio.to_thread(
        current_session_fact,
        state_as(request, AuthAppState).sessions,
        state_as(request, AuthAppState).session_keys,
        state_as(request, AuthAppState).login_passwords,
        token,
        config.cluster_secret,
        home=gateway_state(request).gateway_process.home,
        now=gateway_clock(request).now(),
        cache=gateway_state(request).machine_token_acceptance,
    )
    # meiva:note
    # In the `trusted_network` mode a request the middleware would serve without a credential
    # (a trusted `Host`) reports authenticated too, so the UI skips its login page.
    anonymous_admitted = (
        config.human_auth == "trusted_network"
        and request_host(request.headers.get("Host")) in config.trusted_hosts
    )
    return JSONResponse(content={"authenticated": fact is not None or anonymous_admitted})


# meiva:note
# One listed browser session; `id` is masked unless it is the caller's own.
class BrowserSession(BaseModel):
    id: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_seen_at: datetime
    user_agent: str
    ip: str
    current: bool
    managed: bool


# meiva:note
# Unrevoked, unexpired sessions whose mint is still admitted: the rows
# `current_session_fact` would accept. The sessions of a rotated secret or a password set
# again, and ids without a mint, are dead and omitted.
def _sessions_that_authenticate(
    pool: ConnectionPool,
    keys: SessionKeys,
    passwords: LoginPasswords,
    secret: str,
    *,
    home: Path,
    cache: AcceptanceCache | None = None,
) -> list[WebSessionRow]:
    admitted = session_mints(keys, passwords, secret, home=home, cache=cache)
    return [row for row in list_sessions(pool) if session_mint(row.id) in admitted]


@router.get("/api/auth/sessions")
async def sessions(request: Request) -> list[BrowserSession]:
    """List browser sessions that still authenticate, marking the current cookie.

    A session is listed only while the credential that minted it is current,
    the same test the session check applies, so a security screen never shows
    a dead session as active. Only the request's current session keeps its full
    id; every other row's id is masked to its final 8 characters — enough to
    tell rows apart and to revoke (the revoke endpoint accepts the suffix),
    without exposing the full credential of sessions the caller does not hold.
    Managed-browser sessions are labeled with ``managed`` so they are not
    mistaken for the caller's own.
    """
    current_session_id = request.cookies.get(cookie_name())
    config: GatewayConfig = gateway_state(request).gateway_config
    rows = await asyncio.to_thread(
        _sessions_that_authenticate,
        gateway_state(request).db_pool,
        state_as(request, AuthAppState).session_keys,
        state_as(request, AuthAppState).login_passwords,
        config.cluster_secret,
        home=gateway_state(request).gateway_process.home,
        cache=gateway_state(request).machine_token_acceptance,
    )
    result: list[BrowserSession] = []
    for row in rows:
        is_current = row.id == current_session_id
        result.append(
            BrowserSession(
                id=row.id if is_current else row.id[-8:],
                created_at=row.created_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                last_seen_at=row.last_seen_at,
                user_agent=row.user_agent,
                ip=row.ip,
                current=is_current,
                managed=is_managed_browser_user_agent(row.user_agent),
            )
        )
    return result


@router.post("/api/auth/sessions/{session_id}/revoke")
async def revoke_other_session(session_id: str, request: Request) -> JSONResponse:
    """Revoke a non-current browser session.

    Accepts either the full session id or the 8-character masked suffix shown
    in the sessions list. The suffix fallback only triggers for the exact
    masked form (8 characters) — any other shape is a full id and 404s when
    unknown — and never matches the request's own session (whose only
    revocation path is logout). An ambiguous suffix (more than one other
    active session ends with it) is refused rather than revoked wholesale.
    """
    current_session_id = request.cookies.get(cookie_name())
    if session_id == current_session_id:
        raise HTTPException(
            status_code=409,
            detail="current session must be revoked via logout",
        )
    pool = gateway_state(request).db_pool
    revoked = await asyncio.to_thread(state_as(request, AuthAppState).sessions.revoke, session_id)
    if not revoked and len(session_id) == 8:
        # The masked suffix from the list — nothing else falls back here. The
        # current session is excluded: its only revocation path is logout, and
        # its own suffix must not bypass that guard.
        matches = [
            match
            for match in await asyncio.to_thread(session_ids_with_suffix, pool, session_id)
            if match != current_session_id
        ]
        if len(matches) > 1:
            raise HTTPException(
                status_code=409,
                detail="session suffix is ambiguous",
            )
        if len(matches) == 1:
            revoked = await asyncio.to_thread(
                state_as(request, AuthAppState).sessions.revoke, matches[0]
            )
    if not revoked:
        raise HTTPException(status_code=404, detail="session not found")
    return JSONResponse(content={"ok": True})
