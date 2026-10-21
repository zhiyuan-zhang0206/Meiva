from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from typing import Literal

import pytest
from fastapi import Depends, HTTPException, Request
from pydantic import ValidationError
from starlette.testclient import TestClient

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.domains.gateway import GatewaySettings
from base.host.net.auth import bearer_header, cookie_name
from gateway.http.auth.request_principal import (
    HUMAN_CREDENTIAL_FACTS,
    TRUSTED_NETWORK_FACT,
    require_human_credential,
)
from gateway.http.config import gateway_config_from_boot
from gateway.http.middleware.cors import request_host, trusted_request_hosts
from gateway.tests.agents.gateway_auth import disable_default_gateway_credentials
from tests.fixtures.gateway_config import gateway_test_app, gateway_test_process

CLUSTER_KEY = "trusted-network-test-secret"
ALLOWED_ORIGIN = "http://10.0.0.5:3000"
HOSTS = trusted_request_hosts(
    allowed_origins=(ALLOWED_ORIGIN,), machine_host="10.0.0.5", gateway_url="http://10.0.0.5:8000"
)
DECLARED = {"Host": "10.0.0.5:8000"}


@pytest.fixture(autouse=True)
def anonymous_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    disable_default_gateway_credentials(monkeypatch)


def probe(request: Request) -> dict[str, str | None]:
    principal = request.state.auth_principal
    return {
        "fact": request.state.source_verified_by,
        "principal": None if principal is None else f"{principal.kind}:{principal.subject}",
    }


@contextmanager
def served(
    human_auth: Literal["required", "trusted_network"] | None, machine: MachineIdentity
) -> Generator[TestClient]:
    with gateway_test_process(machine=machine) as process:
        config = replace(
            gateway_config_from_boot(process.config),
            cluster_secret=CLUSTER_KEY,
            allowed_origins=(ALLOWED_ORIGIN,),
            trusted_hosts=HOSTS,
        )
        if human_auth is not None:
            config = replace(config, human_auth=human_auth)
        app = gateway_test_app(process=process, config=config)
        app.add_api_route("/api/probe", probe, methods=["GET", "POST"])
        app.add_api_route(
            "/api/probe-human",
            probe,
            methods=["GET"],
            dependencies=[Depends(require_human_credential)],
        )
        with TestClient(app) as client:
            yield client


def test_required_mode_still_refuses_an_anonymous_request(
    *, machine_identity: MachineIdentity
) -> None:
    with served("required", machine_identity) as client:
        assert client.get("/api/probe", headers=DECLARED).status_code == 401


def test_trusted_network_serves_an_anonymous_request_as_the_administrator(
    *, machine_identity: MachineIdentity
) -> None:
    with served("trusted_network", machine_identity) as client:
        response = client.get("/api/probe", headers=DECLARED)

    assert response.status_code == 200
    assert response.json() == {"fact": TRUSTED_NETWORK_FACT, "principal": "cluster:administrator"}


def test_a_bearer_keeps_its_own_fact_in_trusted_network_mode(
    *, machine_identity: MachineIdentity
) -> None:
    with served("trusted_network", machine_identity) as client:
        response = client.get("/api/probe", headers={**DECLARED, **bearer_header(CLUSTER_KEY)})

    assert response.json()["fact"] == "cluster_bearer"


@pytest.mark.parametrize(
    "authorization", ["Bearer wrong-secret", "Bearer machine-token-revoked", "Basic abc", ""]
)
def test_an_invalid_authorization_header_stays_a_401(
    authorization: str, *, machine_identity: MachineIdentity
) -> None:
    with served("trusted_network", machine_identity) as client:
        response = client.get("/api/probe", headers={**DECLARED, "Authorization": authorization})

    assert response.status_code == 401


def test_an_invalid_session_cookie_still_falls_through_to_the_anonymous_path(
    *, machine_identity: MachineIdentity
) -> None:
    with served("trusted_network", machine_identity) as client:
        response = client.get(
            "/api/probe", headers={**DECLARED, "Cookie": f"{cookie_name()}=stale.value"}
        )

    assert response.json()["fact"] == TRUSTED_NETWORK_FACT


def test_a_foreign_origin_cannot_post_anonymously(*, machine_identity: MachineIdentity) -> None:
    with served("trusted_network", machine_identity) as client:
        foreign = client.post("/api/probe", headers={**DECLARED, "Origin": "https://evil.example"})
        allowed = client.post("/api/probe", headers={**DECLARED, "Origin": ALLOWED_ORIGIN})
        read = client.get("/api/probe", headers={**DECLARED, "Origin": "https://evil.example"})

    assert (foreign.status_code, allowed.status_code, read.status_code) == (403, 200, 200)


@pytest.mark.parametrize("host", ["rebind.attacker.example", "rebind.attacker.example:8000"])
def test_an_unknown_host_is_refused_anonymously(
    host: str, *, machine_identity: MachineIdentity
) -> None:
    with served("trusted_network", machine_identity) as client:
        assert client.get("/api/probe", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize(
    "host", ["10.0.0.5:8000", "localhost:8000", "127.0.0.1", "[::1]:8000", "LOCALHOST"]
)
def test_declared_and_loopback_hosts_are_served_anonymously(
    host: str, *, machine_identity: MachineIdentity
) -> None:
    with served("trusted_network", machine_identity) as client:
        assert client.get("/api/probe", headers={"Host": host}).status_code == 200


def test_an_unknown_host_does_not_affect_a_credentialed_request(
    *, machine_identity: MachineIdentity
) -> None:
    with served("trusted_network", machine_identity) as client:
        response = client.get(
            "/api/probe", headers={"Host": "rebind.attacker.example", **bearer_header(CLUSTER_KEY)}
        )

    assert response.status_code == 200


def test_a_human_only_route_accepts_the_anonymous_trusted_network_request(
    *, machine_identity: MachineIdentity
) -> None:
    with served("trusted_network", machine_identity) as client:
        assert client.get("/api/probe-human", headers=DECLARED).status_code == 200


@pytest.mark.parametrize(
    ("human_auth", "host", "authenticated"),
    [
        ("required", "10.0.0.5:8000", False),
        ("trusted_network", "10.0.0.5:8000", True),
        ("trusted_network", "rebind.attacker.example", False),
    ],
)
def test_auth_check_reports_what_the_middleware_would_admit(
    human_auth: Literal["required", "trusted_network"],
    host: str,
    authenticated: bool,
    *,
    machine_identity: MachineIdentity,
) -> None:
    with served(human_auth, machine_identity) as client:
        response = client.get("/api/auth/check", headers={"Host": host})

    assert response.json() == {"authenticated": authenticated}


def test_trusted_hosts_collect_every_declared_source() -> None:
    assert trusted_request_hosts(
        allowed_origins=("https://console.example", "http://[fd00::2]:3000"),
        machine_host="fd00::1",
        gateway_url="http://gw.internal:8000",
    ) >= {"localhost", "127.0.0.1", "::1", "console.example", "fd00::2", "fd00::1", "gw.internal"}
    assert request_host("[fd00::1]:8000") == "fd00::1"


def test_require_human_credential_accepts_the_trusted_network_fact() -> None:
    assert TRUSTED_NETWORK_FACT in HUMAN_CREDENTIAL_FACTS

    def request(fact: str) -> Request:
        return Request({"type": "http", "headers": [], "state": {"source_verified_by": fact}})

    require_human_credential(request(TRUSTED_NETWORK_FACT))
    with pytest.raises(HTTPException) as refused:
        require_human_credential(request("machine_token:gateway"))
    assert refused.value.status_code == 403


def test_human_auth_defaults_to_trusted_network_and_rejects_unknown_values() -> None:
    assert GatewaySettings().human_auth == "trusted_network"
    assert GatewaySettings.model_validate({"human_auth": "required"}).human_auth == "required"
    with pytest.raises(ValidationError):
        GatewaySettings.model_validate({"human_auth": "open"})


def test_default_policy_skips_login_and_serves_the_api(
    *, machine_identity: MachineIdentity
) -> None:
    with served(None, machine_identity) as client:
        check = client.get("/api/auth/check", headers=DECLARED)
        response = client.get("/api/probe", headers=DECLARED)
    assert check.json() == {"authenticated": True}
    assert response.status_code == 200
    assert response.json() == {"fact": TRUSTED_NETWORK_FACT, "principal": "cluster:administrator"}
