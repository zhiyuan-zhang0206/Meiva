"""Gateway-wide RFC 9457-style error-envelope contract tests."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import httpx
import httpx2
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from base import paths
from base.api_contracts.agent_contract import AgentNotFound, ErrorReason, MeivaAgentError
from base.config.tests.configuration import snapshot_process_config
from base.host.db_authority.api import AcceptanceCache
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from gateway.app import (
    cluster_auth_middleware,
)
from gateway.http.auth import rejection_log
from gateway.http.middleware.error_envelope import request_trace_middleware
from gateway.http.middleware.error_handlers import (
    http_exception_handler,
    meiva_agent_error_handler,
    request_validation_error_handler,
    unhandled_exception_handler,
)
from gateway.lgtm.backend_failure import raise_backend_unavailable
from gateway.schemas import ErrorEnvelope
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_config_of

_middleware_app = FastAPI()
# meiva:note
# isolated test credential.
_SECRET = "test-secret"  # noqa: S105


def _assert_envelope(
    response: httpx.Response | httpx2.Response | Response,
    *,
    status: int,
    code: str,
    retryable: bool,
    reason: str | None = None,
    extensions: set[str] | None = None,
) -> None:
    """Assert the common response contract without coupling extension values."""
    extensions = extensions or set()
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = json.loads(bytes(response.body)) if isinstance(response, Response) else response.json()
    assert set(body) == {
        "type",
        "code",
        "status",
        "detail",
        "retryable",
        "trace_id",
        *extensions,
        *({"reason"} if reason is not None else set()),
    }
    assert body["type"] == "about:blank"
    assert body["code"] == code
    assert body["status"] == status
    assert isinstance(body["detail"], str)
    assert body["retryable"] is retryable
    assert isinstance(body["trace_id"], str) and body["trace_id"]
    if reason is not None:
        assert body["reason"] == reason


@pytest.fixture
def handler_client() -> Iterator[GatewayTestClient]:
    """An isolated app that dispatches every shared gateway error handler."""
    app = FastAPI()
    app.state.gateway_config = gateway_config_of(snapshot_process_config())
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(request_trace_middleware(app.state.gateway_config.rng))
    app.add_exception_handler(MeivaAgentError, meiva_agent_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/agent")
    def agent_error() -> None:
        raise AgentNotFound("agent missing")

    @app.get("/http/{status}")
    def http_error(status: int) -> None:
        raise HTTPException(status_code=status, detail=f"HTTP {status}")

    @app.get("/backend")
    def backend_error() -> None:
        raise_backend_unavailable(httpx.ConnectError("offline"), backend="loki")

    @app.get("/validation")
    def validation_error(count: int) -> None:
        del count

    @app.get("/exception")
    def unhandled_error() -> None:
        raise RuntimeError("boom")

    with GatewayTestClient(app, raise_server_exceptions=False) as client:
        yield client


# meiva:note
# The app config's seeded generator decides the fallback correlation id of a request: the first
# draw is the middleware's, and the second request gets a different one.
def test_a_request_without_a_trace_gets_the_id_its_app_generator_draws(
    handler_client: GatewayTestClient,
) -> None:
    response = handler_client.get("/agent")
    expected = UUID(int=random.Random(0).getrandbits(128), version=4).hex  # noqa: S311
    assert response.json()["trace_id"] == expected
    assert handler_client.get("/agent").json()["trace_id"] != expected


@pytest.mark.parametrize(
    ("path", "status", "code", "retryable", "reason", "extensions"),
    [
        ("/agent", 404, "agent_not_found", False, "agent_not_found", set[str]()),
        ("/http/404", 404, "http_404", False, None, set[str]()),
        ("/backend", 503, "http_503", True, None, set[str]()),
        ("/validation?count=not-an-int", 422, "validation_error", False, None, {"errors"}),
        ("/exception", 500, "internal_error", False, None, set[str]()),
    ],
)
def test_global_handlers_emit_typed_envelopes(
    handler_client: GatewayTestClient,
    path: str,
    status: int,
    code: str,
    retryable: bool,
    reason: str | None,
    extensions: set[str],
) -> None:
    """Every handler keeps its status while exposing one typed response shape."""
    origin = handler_client.app.state.gateway_config.allowed_origins[0]
    response = handler_client.get(path, headers={"Origin": origin})
    _assert_envelope(
        response,
        status=status,
        code=code,
        retryable=retryable,
        reason=reason,
        extensions=extensions,
    )
    if path == "/backend":
        assert response.headers["retry-after"] == "1"
    if path == "/validation?count=not-an-int":
        assert isinstance(response.json()["errors"], list)
    if path == "/exception":
        assert response.headers["access-control-allow-origin"] == origin


def _request(
    *,
    method: str = "GET",
    path: str = "/api/agents",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 1),
) -> Request:
    """Build the smallest request shape accepted by the direct middleware calls.

    The scope carries its application-owned rejection counters but no session
    store. A request without a session cookie must never reach `db_pool`, so
    bearer and anonymous requests pay no session lookup.
    """
    _middleware_app.state.gateway_config = replace(
        gateway_config_of(snapshot_process_config()),
        cluster_secret=_SECRET,
        human_auth="required",
    )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers or [],
            "client": client,
            "server": ("testserver", 80),
            "state": {"trace_id": "0" * 32},
            "app": _middleware_app,
        }
    )


@pytest.fixture(autouse=True)
def _auth401_state() -> None:
    """Each middleware test gets its own gateway-lifetime counters and served root."""
    _middleware_app.state.auth401_log = rejection_log.AuthRejectionLog()
    machine_token_acceptance: AcceptanceCache = {}
    _middleware_app.state.machine_token_acceptance = machine_token_acceptance
    _middleware_app.state.gateway_process = SimpleNamespace(home=paths.meiva_home())


def _unauthorized_auth_response(request: Request) -> Response:
    async def call_next(_request: Request) -> Response:
        raise AssertionError("authentication middleware must short-circuit")

    return asyncio.run(cluster_auth_middleware(request, call_next))


def _auth401_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "gateway.http.auth.rejection_log"
        and record.getMessage().startswith("auth 401:")
    ]


def test_auth_middleware_uses_envelope() -> None:
    """Unauthorized short-circuits remain 401 but expose the shared contract."""

    async def call_next(_request: Request) -> Response:
        raise AssertionError("authentication middleware must short-circuit")

    response = asyncio.run(cluster_auth_middleware(_request(), call_next))
    _assert_envelope(response, status=401, code="authentication_required", retryable=False)


def test_auth_middleware_requires_cluster_auth_for_alert_reads() -> None:
    """The webhook exemption must not expose the alerts read API."""

    async def call_next(_request: Request) -> Response:
        raise AssertionError("unauthenticated alert reads must short-circuit")

    response = asyncio.run(cluster_auth_middleware(_request(path="/api/alerts"), call_next))
    _assert_envelope(response, status=401, code="authentication_required", retryable=False)


def test_auth_middleware_leaves_alert_webhook_auth_to_router() -> None:
    """Only the POST alert webhook reaches its router without cluster auth."""

    async def call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(
        cluster_auth_middleware(
            _request(method="POST", path="/api/alerts"),
            call_next,
        )
    )
    assert response.status_code == 204


def test_auth_middleware_accepts_bearer_for_alert_reads() -> None:
    """Authenticated alert reads continue through ordinary cluster auth."""

    async def call_next(_request: Request) -> Response:
        return Response(status_code=204)

    response = asyncio.run(
        cluster_auth_middleware(
            _request(
                path="/api/alerts",
                headers=[(b"authorization", f"Bearer {_SECRET}".encode())],
            ),
            call_next,
        )
    )
    assert response.status_code == 204


@pytest.mark.parametrize(
    "path",
    [
        "/api/alerts/stream",
        "/api/system/all",
        "/api/agents/1/events/stream",
        "/api/system",
        "/api/agents/1/system",
    ],
)
def test_auth_middleware_logs_sse_poll_401_at_debug(
    path: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Stale EventSource reconnects stay forensic-only without changing the response."""
    caplog.set_level(logging.DEBUG, logger="gateway.http.auth.rejection_log")

    response = _unauthorized_auth_response(
        _request(path=path, headers=[(b"user-agent", b"stale-browser")])
    )
    _assert_envelope(response, status=401, code="authentication_required", retryable=False)
    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.DEBUG]
    assert f"path={path}" in records[0].getMessage()
    assert "client=127.0.0.1" in records[0].getMessage()
    assert "ua=stale-browser" in records[0].getMessage()


def test_auth_middleware_logs_browser_ua_401_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """A console tab whose session died in a rollout retries every open request
    at once; the 401 is visible in the UI and still counted in the aggregate,
    but never WARNING-throttled per path (2026-10-03 triage #7)."""
    caplog.set_level(logging.DEBUG, logger="gateway.http.auth.rejection_log")
    browser_ua = (
        b"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        b"(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    )

    response = _unauthorized_auth_response(_request(headers=[(b"user-agent", browser_ua)]))
    _assert_envelope(response, status=401, code="authentication_required", retryable=False)
    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.DEBUG]
    assert "path=/api/agents" in records[0].getMessage()
    assert "client=127.0.0.1" in records[0].getMessage()
    assert "Mozilla/5.0" in records[0].getMessage()
    assert _middleware_app.state.auth401_log.total == 1  # the aggregate still counts it
    assert _middleware_app.state.auth401_log.last_warn == {}  # and it burns no WARNING budget


def test_auth_middleware_warns_on_first_non_stream_401(caplog: pytest.LogCaptureFixture) -> None:
    """A new client/path source remains visible once at WARNING."""
    caplog.set_level(logging.DEBUG, logger="gateway.http.auth.rejection_log")

    response = _unauthorized_auth_response(_request(headers=[(b"user-agent", b"curl/8.1")]))
    _assert_envelope(response, status=401, code="authentication_required", retryable=False)
    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.WARNING]
    assert "path=/api/agents" in records[0].getMessage()
    assert "client=127.0.0.1" in records[0].getMessage()
    assert "ua=curl/8.1" in records[0].getMessage()


def test_auth_middleware_suppresses_immediate_non_stream_401_repeat(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repeated client/path key is downgraded and counted during cooldown."""
    caplog.set_level(logging.DEBUG, logger="gateway.http.auth.rejection_log")
    request = _request()

    _unauthorized_auth_response(request)
    caplog.clear()
    response = _unauthorized_auth_response(request)

    _assert_envelope(response, status=401, code="authentication_required", retryable=False)
    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.DEBUG]
    assert "suppressed" in records[0].getMessage()
    assert _middleware_app.state.auth401_log.suppressed[("127.0.0.1", "/api/agents")] == 1


# meiva:note
# The next warning reports repeats hidden during the elapsed cooldown.
def test_auth_rejection_log_warns_after_cooldown_with_suppressed_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gateway.http.auth.rejection_log")
    log = _middleware_app.state.auth401_log
    request = _request()

    log.log(request, 100.0)
    log.log(request, 100.0)
    caplog.clear()
    log.log(request, 401.0)

    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.WARNING]
    assert "suppressed 1 repeats in the last 300s" in records[0].getMessage()


# meiva:note
# A client/path key idle for two windows stops consuming throttle state.
def test_auth_rejection_log_prunes_idle_401_throttle_keys() -> None:
    log = _middleware_app.state.auth401_log
    stale_key = ("127.0.0.1", "/api/agents")

    log.log(_request(), 100.0)
    log.log(_request(), 100.0)
    log.log(_request(path="/api/alerts"), 701.0)

    assert stale_key not in log.last_warn
    assert stale_key not in log.suppressed


def test_auth_middleware_throttles_non_stream_401s_per_client_and_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warning for one client/path key does not hide either neighboring key."""
    caplog.set_level(logging.DEBUG, logger="gateway.http.auth.rejection_log")
    _unauthorized_auth_response(_request())
    caplog.clear()

    responses = [
        _unauthorized_auth_response(_request(client=("127.0.0.2", 1))),
        _unauthorized_auth_response(_request(path="/api/alerts")),
    ]

    for response in responses:
        _assert_envelope(
            response,
            status=401,
            code="authentication_required",
            retryable=False,
        )
    records = _auth401_records(caplog)
    assert [record.levelno for record in records] == [logging.WARNING, logging.WARNING]
    messages = [record.getMessage() for record in records]
    assert any("path=/api/agents client=127.0.0.2" in message for message in messages)
    assert any("path=/api/alerts client=127.0.0.1" in message for message in messages)


def test_auth401_aggregate_counts_every_rejection() -> None:
    """The process-local counter increments on EVERY middleware rejection —
    SSE flood paths and throttled repeats included (the log severity is
    throttled, the count must not be; task #1712)."""
    assert _middleware_app.state.auth401_log.total == 0

    _unauthorized_auth_response(_request(path="/api/events/stream"))
    _unauthorized_auth_response(_request(path="/api/events/stream"))
    _unauthorized_auth_response(_request(path="/api/agents"))
    _unauthorized_auth_response(_request(path="/api/agents"))

    assert _middleware_app.state.auth401_log.total == 4


def test_auth401_drain_returns_count_once_and_resets() -> None:
    """drain_auth401_count() reads the aggregate and resets — the flusher's
    read-and-reset must not double-count a window."""
    _unauthorized_auth_response(_request())
    _unauthorized_auth_response(_request())

    assert _middleware_app.state.auth401_log.drain() == 2
    assert _middleware_app.state.auth401_log.drain() == 0
    _unauthorized_auth_response(_request())
    assert _middleware_app.state.auth401_log.drain() == 1


def test_auth401_emit_aggregate_event(emitted_events: EmittedEvents) -> None:
    """emit_auth401_count sends one `auth401_rejected` telemetry event with the
    window count; a zero window emits nothing (no zero datapoints)."""
    rejection_log.emit_auth401_count(0)
    assert emitted_events.names() == []

    rejection_log.emit_auth401_count(17)
    assert [(e.category, e.event_name, e.attributes) for e in emitted_events] == [
        ("telemetry", "auth401_rejected", {"count": 17})
    ]


def test_error_envelope_schema_round_trips_and_omits_unset_reason() -> None:
    """The schema preserves the six common fields and opt-in SDK reason field."""
    envelope = ErrorEnvelope(
        code="validation_error",
        status=422,
        detail="Request validation failed",
        retryable=False,
        trace_id="f" * 32,
    )
    dumped = envelope.model_dump(mode="json", exclude_none=True)
    assert "reason" not in dumped
    assert ErrorEnvelope.model_validate(dumped) == envelope

    agent_envelope = envelope.model_copy(
        update={"code": "agent_not_found", "reason": ErrorReason.AGENT_NOT_FOUND}
    )
    assert agent_envelope.model_dump(mode="json", exclude_none=True)["reason"] == "agent_not_found"
