"""Guarded routing must reject legacy servers and unverified creation identities."""

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.agents.creation import scoped_creation_key
from gateway.http.auth.request_principal import AuthPrincipal, principal_key
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
)
from gateway.tests.support.runner_ops.ops_stub import RunnerOps, completed, launched, runner_ops

PATH = "/api/keyed/v1/agents"
SECRET = "guarded-test-secret"  # noqa: S105 -- isolated auth fixture
HEADERS = {"Idempotency-Key": "intent", "Idempotency-Scope": "principal-v1"}


@pytest.fixture
def runner(
    gateway_data_plane: None, db_conn: psycopg.Connection, set_machine_identity: Any
) -> Generator[RunnerOps]:
    """The runner `local-test`, which accepts every launch it is asked for."""
    set_machine_identity(role="agent-runner", name="local-test")
    replies = {"spawn-launch": launched, "launch-reconcile": completed({"wake_published": False})}
    with runner_ops(db_conn, "local-test", replies) as value:
        yield value


@pytest.fixture
def client(
    runner: RunnerOps, *, machine_identity: MachineIdentityReader
) -> Generator[GatewayTestClient]:
    with gateway_test_client(
        cluster_secret=SECRET,
        headers={"Authorization": f"Bearer {SECRET}"},
        machine=machine_identity,
    ) as value:
        yield value


def _count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM agents").fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "", "Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x" * 129, "Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "intent"},
        {"Idempotency-Key": "intent", "Idempotency-Scope": "legacy"},
    ],
)
def test_invalid_admission_has_no_birth(
    client: TestClient, db_conn: psycopg.Connection, headers: dict[str, str]
) -> None:
    assert client.post(PATH, json={}, headers=headers).status_code == 422
    assert _count(db_conn) == 0


def test_fork_without_source_checkpoint_has_no_birth(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    parent = client.post(PATH, json={}, headers={**HEADERS, "Idempotency-Key": "parent"})
    assert parent.status_code == 201, parent.text
    assert (
        client.post(PATH, json={"fork_from": parent.json()["id"]}, headers=HEADERS).status_code
        == 409
    )
    assert _count(db_conn) == 1


def test_lost_response_and_concurrency_replay_one_birth(
    client: TestClient,
    db_conn: psycopg.Connection,
) -> None:
    body = {"machine": "local-test", "prompt": "one goal", "prompt_source": "user"}
    accepted = client.post(PATH, json=body, headers=HEADERS)
    assert accepted.status_code == 201, accepted.text
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(client.post, PATH, json=body, headers=HEADERS) for _ in range(3)]
        responses = [future.result() for future in futures]
    assert all(response.status_code == 201 for response in responses)
    assert all(response.json()["id"] == accepted.json()["id"] for response in responses)
    assert _count(db_conn) == 1
    key = principal_key(AuthPrincipal("cluster", "administrator"), "POST", PATH, "intent")
    row = db_conn.execute("SELECT creation_key FROM agent_creation_snapshots").fetchone()
    assert row is not None and row[0] == key
    assert client.post(PATH, json={**body, "prompt": "changed"}, headers=HEADERS).status_code == 409
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_revoked_credential_cannot_replay_guarded_receipt(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
) -> None:
    assert client.post(PATH, json={"machine": "local-test"}, headers=HEADERS).status_code == 201
    with gateway_test_client(
        process=client.app.state.gateway_process,
        config=replace(
            client.app.state.gateway_config,
            cluster_secret="rotated-guarded-secret",  # noqa: S106 -- isolated test credential
        ),
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as rotated:
        assert rotated.post(PATH, json={}, headers=HEADERS).status_code == 401
    assert _count(db_conn) == 1


def test_scoped_creation_key_binds_the_principal_and_the_operation_path() -> None:
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/keyed/v1/agents",
            "headers": [(b"idempotency-scope", b"principal-v1")],
        }
    )
    principal = AuthPrincipal("cluster", "administrator")
    request.state.auth_principal = principal
    assert scoped_creation_key(request, "prior", operation_path=PATH) == principal_key(
        principal, "POST", PATH, "prior"
    )
