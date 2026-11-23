from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.context import Context, attach, detach
from opentelemetry.propagate import get_global_textmap
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.routing import compile_path
from starlette.types import Receive, Scope, Send

from base.api_contracts.contracts import ALERT_AGENT_WEBHOOK_PATH
from base.host.db_authority.model import AuthorityRefusedError
from base.host.net.auth import cookie_name
from gateway.http.auth.request_principal import TRUSTED_NETWORK_FACT, cluster_credential
from gateway.http.auth.router import AuthAppState
from gateway.http.auth.session_store import SessionTouchThrottle, touch_session
from gateway.http.config import GatewayConfig, app_state_as, state_as
from gateway.http.middleware.cors import request_host
from gateway.http.middleware.error_envelope import error_response

_AUTH_BYPASS_PATHS: frozenset[str] = frozenset(
    {
        "/api/health",
        "/api/auth/login",
        "/api/auth/login-ticket/redeem",
        "/api/auth/check",
        "/api/auth/logout",
    }
)
_AUTH_BYPASS_METHOD_PATHS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/alerts"),
        ("POST", ALERT_AGENT_WEBHOOK_PATH),
    }
)
_AUTH_BYPASS_ROUTES = tuple(
    (method, compile_path(path)[0]) for method, path in _AUTH_BYPASS_METHOD_PATHS
)
_STATE_CHANGING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class TraceIngressApp(FastAPI):
    def _accept_machine_parent(self, scope: Scope) -> bool:
        authorization = Headers(scope=scope).get("authorization")
        if authorization is None:
            return False
        state = app_state_as(self, AuthAppState)
        try:
            fact = cluster_credential(
                authorization,
                state.gateway_config.cluster_secret,
                home=state.gateway_process.home,
                cache=state.machine_token_acceptance,
            )
        except (AuthorityRefusedError, OSError):
            return False
        return fact is not None and fact.startswith("machine_token:")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await super().__call__(scope, receive, send)
            return
        fields = {name.lower().encode("ascii") for name in get_global_textmap().fields}
        headers = Headers(scope=scope).raw
        if any(name.lower() in fields for name, _ in headers) and not self._accept_machine_parent(
            scope
        ):
            scope = {
                **scope,
                "headers": [(name, value) for name, value in headers if name.lower() not in fields],
            }
        token = attach(Context())
        try:
            await super().__call__(scope, receive, send)
        finally:
            detach(token)


async def _cookie_session(request: Request, secret: str) -> tuple[str, str] | None:
    state = state_as(request, AuthAppState)

    from gateway.http.auth.request_principal import current_session_fact

    cookie_token = request.cookies.get(cookie_name())
    if not cookie_token:
        return None
    fact = await asyncio.to_thread(
        current_session_fact,
        state.sessions,
        state.session_keys,
        state.login_passwords,
        cookie_token,
        secret,
        home=state.gateway_process.home,
        now=state.clock.now(),
        cache=state.machine_token_acceptance,
    )
    return None if fact is None else (cookie_token, fact)


# meiva:note
# True for a state-changing request whose `Origin` is not an allowed browser origin.
def _foreign_origin(request: Request, config: GatewayConfig) -> bool:
    origin = request.headers.get("Origin")
    return (
        request.method in _STATE_CHANGING_METHODS
        and origin is not None
        and origin not in config.allowed_origins
    )


def _origin_refused() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": "origin not allowed"},
        headers={"Vary": "Origin"},
    )


# meiva:note
# With `human_auth = trusted_network`, a request that presents no valid session cookie and
# no Authorization header at all is served as the cluster administrator (fact `trusted_network`). A browser page is
# the one way to reach that listener without the operator choosing to, so these anonymous
# requests, and only these, also face the Origin and Host checks. An Authorization header
# that does not verify is a client error and stays a 401, so a misconfigured machine fails
# now instead of working silently until the mode is switched back. A stale session cookie
# is not: a browser cannot clear it.
# meiva:link gateway/http/auth/docs/web-sessions.md
async def cluster_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    state = state_as(request, AuthAppState)

    from gateway.http.auth.request_principal import AuthPrincipal

    request.state.auth_principal = None
    request.state.source_verified_by = None
    config: GatewayConfig = state.gateway_config
    secret = config.cluster_secret

    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path in _AUTH_BYPASS_PATHS or any(
        request.method == method and pattern.fullmatch(path)
        for method, pattern in _AUTH_BYPASS_ROUTES
    ):
        return await call_next(request)

    session = await _cookie_session(request, secret)
    if session is not None:
        cookie_token, session_fact = session
        request.state.auth_principal = AuthPrincipal("cluster", "administrator")
        request.state.source_verified_by = session_fact
        if _foreign_origin(request, config):
            return _origin_refused()
        touch: SessionTouchThrottle = state.session_touch
        if touch.due(
            cookie_token,
            state.clock.monotonic(),
            stale_after_s=config.session_ttl_seconds,
        ):
            await asyncio.to_thread(
                touch_session,
                state.db_pool,
                cookie_token,
            )
        return await call_next(request)

    verified_by = cluster_credential(
        request.headers.get("Authorization"),
        secret,
        home=state.gateway_process.home,
        cache=state.machine_token_acceptance,
    )
    if verified_by is not None:
        request.state.auth_principal = AuthPrincipal("cluster", "administrator")
        request.state.source_verified_by = verified_by
        return await call_next(request)

    if config.human_auth == "trusted_network" and request.headers.get("Authorization") is None:
        if _foreign_origin(request, config):
            return _origin_refused()
        if request_host(request.headers.get("Host")) not in config.trusted_hosts:
            return JSONResponse(status_code=403, content={"detail": "host not allowed"})
        request.state.auth_principal = AuthPrincipal("cluster", "administrator")
        request.state.source_verified_by = TRUSTED_NETWORK_FACT
        return await call_next(request)

    state.auth401_log.log(request, state.clock.monotonic())
    return error_response(
        request,
        code="authentication_required",
        status=401,
        detail="authentication required",
        retryable=False,
    )
