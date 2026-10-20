from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from base import paths
from base.config.domains.gateway import GatewaySettings
from base.host.net.auth import bearer_header
from gateway.app import cluster_auth_middleware
from gateway.http.auth import rejection_log
from gateway.http.auth.request_principal import (
    HUMAN_CREDENTIAL_FACTS,
    TRUSTED_NETWORK_FACT,
    AuthPrincipal,
    SessionKeys,
    require_human_credential,
)
from gateway.http.auth.router import check
from gateway.http.config import GatewayConfig
from gateway.http.middleware.cors import request_host, trusted_request_hosts

CLUSTER_KEY = "trusted-network-test-secret"
ALLOWED_ORIGIN = "http://10.0.0.5:3000"
HOSTS = trusted_request_hosts(
    allowed_origins=(ALLOWED_ORIGIN,), machine_host="10.0.0.5", gateway_url="http://10.0.0.5:8000"
)


def _app(human_auth: Literal["required", "trusted_network"]) -> FastAPI:
    app = FastAPI()
    app.state.gateway_config = GatewayConfig(
        cluster_secret=CLUSTER_KEY,
        session_ttl_seconds=3600,
        mcp_endpoint_enabled=False,
        allowed_origins=(ALLOWED_ORIGIN,),
        browser_origin="",
        session_cookie_secure=False,
        memory_root=Path("/nonexistent/memory"),
        human_auth=human_auth,
        trusted_hosts=HOSTS,
    )
    app.state.sessions = None
    app.state.session_keys = SessionKeys()
    app.state.auth401_log = rejection_log.AuthRejectionLog()
    app.state.machine_token_acceptance = {}
    app.state.gateway_process = SimpleNamespace(home=paths.meiva_home())
    return app


def _request(
    app: FastAPI,
    *,
    method: str = "GET",
    host: str | None = "10.0.0.5:8000",
    origin: str | None = None,
    authorization: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        headers.append((b"host", host.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/api/agents",
            "raw_path": b"/api/agents",
            "query_string": b"",
            "headers": headers,
            "client": ("10.0.0.9", 1),
            "server": ("10.0.0.5", 8000),
            "state": {},
            "app": app,
        }
    )


def _run(request: Request) -> tuple[Response, Request | None]:
    passed: list[Request] = []

    async def call_next(forwarded: Request) -> Response:
        passed.append(forwarded)
        return JSONResponse({"served": True})

    response = asyncio.run(cluster_auth_middleware(request, call_next))
    return response, passed[0] if passed else None


def test_required_mode_still_refuses_an_anonymous_request() -> None:
    response, passed = _run(_request(_app("required")))

    assert (response.status_code, passed) == (401, None)


def test_trusted_network_serves_an_anonymous_request_as_the_administrator() -> None:
    request = _request(_app("trusted_network"))

    response, passed = _run(request)

    assert response.status_code == 200
    assert passed is not None
    assert passed.state.source_verified_by == TRUSTED_NETWORK_FACT
    assert passed.state.auth_principal == AuthPrincipal("cluster", "administrator")


def test_a_bearer_keeps_its_own_fact_in_trusted_network_mode() -> None:
    request = _request(
        _app("trusted_network"), authorization=bearer_header(CLUSTER_KEY)["Authorization"]
    )

    _, passed = _run(request)

    assert passed is not None
    assert passed.state.source_verified_by == "cluster_bearer"


def test_a_foreign_origin_cannot_post_anonymously() -> None:
    app = _app("trusted_network")

    foreign, foreign_passed = _run(_request(app, method="POST", origin="https://evil.example"))
    allowed, allowed_passed = _run(_request(app, method="POST", origin=ALLOWED_ORIGIN))
    read, read_passed = _run(_request(app, origin="https://evil.example"))

    assert (foreign.status_code, foreign_passed) == (403, None)
    assert (allowed.status_code, allowed_passed is not None) == (200, True)
    assert (read.status_code, read_passed is not None) == (200, True)


@pytest.mark.parametrize("host", ["rebind.attacker.example", "rebind.attacker.example:8000", None])
def test_an_unknown_host_is_refused_anonymously(host: str | None) -> None:
    response, passed = _run(_request(_app("trusted_network"), host=host))

    assert (response.status_code, passed) == (403, None)


@pytest.mark.parametrize(
    "host", ["10.0.0.5:8000", "localhost:8000", "127.0.0.1", "[::1]:8000", "LOCALHOST"]
)
def test_declared_and_loopback_hosts_are_served_anonymously(host: str) -> None:
    response, _ = _run(_request(_app("trusted_network"), host=host))

    assert response.status_code == 200


def test_an_unknown_host_does_not_affect_a_credentialed_request() -> None:
    request = _request(
        _app("trusted_network"),
        host="rebind.attacker.example",
        authorization=bearer_header(CLUSTER_KEY)["Authorization"],
    )

    response, _ = _run(request)

    assert response.status_code == 200


def test_trusted_hosts_collect_every_declared_source() -> None:
    assert trusted_request_hosts(
        allowed_origins=("https://console.example", "http://[fd00::2]:3000"),
        machine_host="fd00::1",
        gateway_url="http://gw.internal:8000",
    ) >= {"localhost", "127.0.0.1", "::1", "console.example", "fd00::2", "fd00::1", "gw.internal"}
    assert request_host("[fd00::1]:8000") == "fd00::1"


def test_require_human_credential_accepts_the_trusted_network_fact() -> None:
    assert TRUSTED_NETWORK_FACT in HUMAN_CREDENTIAL_FACTS

    trusted = _request(_app("required"))
    trusted.state.source_verified_by = TRUSTED_NETWORK_FACT
    require_human_credential(trusted)

    machine = _request(_app("required"))
    machine.state.source_verified_by = "machine_token:gateway"
    with pytest.raises(HTTPException) as refused:
        require_human_credential(machine)
    assert refused.value.status_code == 403


def test_human_auth_defaults_to_required_and_rejects_unknown_values() -> None:
    assert GatewaySettings().human_auth == "required"
    with pytest.raises(ValidationError):
        GatewaySettings.model_validate({"human_auth": "open"})


@pytest.mark.parametrize(
    ("human_auth", "host", "authenticated"),
    [
        ("required", "10.0.0.5:8000", False),
        ("trusted_network", "10.0.0.5:8000", True),
        ("trusted_network", "rebind.attacker.example", False),
    ],
)
def test_auth_check_reports_what_the_middleware_would_admit(
    human_auth: Literal["required", "trusted_network"], host: str, authenticated: bool
) -> None:
    response = asyncio.run(check(_request(_app(human_auth), host=host)))

    assert json.loads(bytes(response.body)) == {"authenticated": authenticated}
