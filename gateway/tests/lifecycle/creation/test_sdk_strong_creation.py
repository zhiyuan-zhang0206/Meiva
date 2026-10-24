"""The actual SDK keeps guarded identity across response loss and router downgrade."""

from collections.abc import Generator
from dataclasses import replace
from functools import partial
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import meiva
import meiva.agents
from base.api_contracts.agent_contract import GatewayUnavailable
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.net.tests.retry_waits import retry_waits as retry_waits
from gateway.agents import router as agent_router
from gateway.app import lifespan
from gateway.tests.agents.gateway_auth import without_credentials
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.auth.sdk_transport import AppTransport, wired_sdk
from gateway.tests.lifecycle.creation.test_guarded_creation import PATH, SECRET
from gateway.tests.lifecycle.creation.test_guarded_creation import client as client
from gateway.tests.lifecycle.creation.test_guarded_creation import runner as runner
from meiva.gateway_client import spawn


@pytest.fixture
def wire(client: GatewayTestClient) -> Generator[AppTransport]:
    """The SDK's gateway client, wired into the guarded-creation app through a recording transport."""
    with wired_sdk(client) as transport:
        yield transport


def sdk(**kwargs: Any) -> int:
    args: dict[str, Any] = {
        "spawner": "user",
        "prompt": "one goal",
        "fork_from": None,
        "prompt_source": "user",
        "machine": "local-test",
        "idempotency_key": "sdk-intent",
    }
    return spawn(**(args | kwargs))


def _older(current: FastAPI) -> FastAPI:
    older = FastAPI(
        lifespan=partial(
            lifespan,
            process=current.state.gateway_process,
            process_ownership="borrowed",
        ),
        middleware=current.user_middleware,
    )
    older.state.gateway_config = current.state.gateway_config
    older.state.gateway_process = current.state.gateway_process
    older.state.catalog = current.state.catalog
    older.exception_handlers = current.exception_handlers.copy()
    routes = APIRouter()
    routes.routes = [
        route for route in agent_router.router.routes if getattr(route, "path", None) != PATH
    ]
    older.include_router(routes)
    return older


@pytest.mark.usefixtures("retry_waits")
def test_sdk_lost_response_downgrade_and_restore_one_birth(
    client: GatewayTestClient, db_conn: psycopg.Connection, wire: AppTransport
) -> None:
    wire.losing_responses = True
    with pytest.raises(GatewayUnavailable):
        sdk()
    lost_attempts = len(wire.requests)
    assert lost_attempts > 1
    wire.losing_responses = False
    row = db_conn.execute("SELECT id FROM agents").fetchone()
    assert row is not None
    original = row[0]
    assert isinstance(client.app, FastAPI)
    with gateway_test_client(
        _older(client.app), headers={"Authorization": f"Bearer {SECRET}"}
    ) as old:
        wire.target = old
        with pytest.raises(httpx.HTTPStatusError) as error:
            sdk()
        assert error.value.response.status_code in (404, 405)
    wire.target = client
    assert sdk() == original
    assert len(wire.requests) == lost_attempts + 2
    assert len(wire.distinct()) == 1
    assert _birth_counts(db_conn) == (1, 1, 1)
    assert _refused_status(spawner="agent:999") == 409
    assert _refused_status(prompt="changed") == 409
    assert _birth_counts(db_conn)[0] == 1
    assert {request.method for request in wire.requests} == {"POST"}


def _refused_status(**options: Any) -> int:
    with pytest.raises(httpx.HTTPStatusError) as refused:
        sdk(**options)
    return refused.value.response.status_code


# meiva:note
# Agents, spawn audit events and inbound messages: one birth leaves one of each.
def _birth_counts(db_conn: psycopg.Connection) -> tuple[int, int, int]:
    counts: list[int] = []
    for query in (
        "SELECT count(*) FROM agents",
        "SELECT count(*) FROM audit_events WHERE event_name='spawn'",
        "SELECT count(*) FROM inbound_messages",
    ):
        row = db_conn.execute(query).fetchone()
        assert row is not None
        counts.append(row[0])
    return counts[0], counts[1], counts[2]


def test_old_router_never_accepts_or_falls_back(
    client: GatewayTestClient, db_conn: psycopg.Connection, wire: AppTransport
) -> None:
    assert isinstance(client.app, FastAPI)
    with gateway_test_client(
        _older(client.app), headers={"Authorization": f"Bearer {SECRET}"}
    ) as old:
        wire.target = old
        with pytest.raises(httpx.HTTPStatusError):
            sdk()
    assert [request.url.path for request in wire.requests] == [PATH]
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)


def test_bearer_and_cookie_share_principal_but_revocation_blocks_replay(
    client: GatewayTestClient, db_conn: psycopg.Connection, wire: AppTransport
) -> None:
    original = sdk()
    login = client.post("/api/auth/login", json={"password": SECRET})
    assert login.status_code == 200
    client.headers.pop("Authorization")
    assert sdk() == original
    with gateway_test_client(
        process=client.app.state.gateway_process,
        config=replace(
            client.app.state.gateway_config,
            cluster_secret="rotated-sdk-strong-secret",  # noqa: S106 -- isolated test credential
        ),
        cookies=dict(client.cookies),
    ) as rotated:
        without_credentials(rotated)
        wire.target = rotated
        with pytest.raises(httpx.HTTPStatusError) as revoked:
            sdk()
    assert revoked.value.response.status_code == 401
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)


@pytest.mark.usefixtures("sdk_environment")
def test_changed_sdk_context_same_principal_key_conflicts(
    client: TestClient,
    db_conn: psycopg.Connection,
    wire: AppTransport,
    machine_identity: MachineIdentity,
) -> None:
    machine_identity.set(name="local-test")
    original = meiva.agents.spawn(prompt="same raw inputs", idempotency_key="context")
    machine_identity.set(name="another-machine")
    with pytest.raises(httpx.HTTPStatusError) as changed:
        meiva.agents.spawn(prompt="same raw inputs", idempotency_key="context")
    assert changed.value.response.status_code == 409
    assert db_conn.execute("SELECT id FROM agents").fetchall() == [(original,)]
    assert {request.url.path for request in wire.requests} == {PATH}
