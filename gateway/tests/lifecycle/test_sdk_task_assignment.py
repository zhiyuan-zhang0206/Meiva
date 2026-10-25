import json
from typing import Any

import httpx
import psycopg
import pytest

from base.agents.context.identity import ExternalLease
from base.api_contracts.agent_contract import GatewayUnavailable
from base.api_contracts.idempotency import keyed_headers
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.net.tests.retry_waits import retry_waits as retry_waits
from gateway.agents.task_assignment import router as assignments
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.auth.sdk_transport import wired_sdk
from gateway.tests.lifecycle.test_task_assignments import HEADERS, PATH, SECRET
from gateway.tests.lifecycle.test_task_assignments import body as body
from gateway.tests.lifecycle.test_task_assignments import client as client
from gateway.tests.lifecycle.test_task_assignments import runner as runner
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet import task_registry
from meiva_builtins.plugins.meiva_fleet.task_registry import TaskAssignment


def _sdk(body: dict[str, Any], **overrides: Any) -> TaskAssignment:
    pin_agent(body["actor_agent_id"])
    return task_registry.create_and_assign(
        body["task"]["title"],
        body["task"]["description"],
        parent=body["task"]["parent"],
        machine="local-test",
        operation_key="sdk-compound",
        **overrides,
    )


@pytest.mark.usefixtures("retry_waits")
def test_real_sdk_response_loss_keeps_exact_body_path_key_and_pair(
    client: GatewayTestClient, db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    with wired_sdk(client) as wire:
        wire.losing_responses = True
        with pytest.raises(GatewayUnavailable):
            _sdk(body)
        lost_attempts = len(wire.requests)
        assert lost_attempts > 1
        wire.losing_responses = False
        assignment = _sdk(body)
    original = db_conn.execute("SELECT result FROM task_assignment_receipts").fetchone()
    assert original is not None
    assert isinstance(assignment, TaskAssignment)
    task, agent_id = assignment
    assert assignment.task is task and assignment.agent_id == agent_id
    assert task.id == original[0]["task"]["id"] and agent_id == original[0]["agent_id"]
    assert len(wire.requests) == lost_attempts + 1
    assert len(wire.distinct()) == 1
    first = wire.requests[0]
    assert first.url.path == PATH
    assert {name: first.headers[name] for name in (*HEADERS, *keyed_headers("sdk-compound"))} == {
        **HEADERS,
        **keyed_headers("sdk-compound"),
    }
    assert db_conn.execute("SELECT count(*) FROM task_assignment_receipts").fetchone() == (1,)


def test_unavailable_atomic_route_refuses_without_separate_spawn(
    body: dict[str, Any], *, machine_identity: MachineIdentityReader
) -> None:
    with gateway_test_client(
        cluster_secret=SECRET,
        headers={"Authorization": f"Bearer {SECRET}"},
        machine=machine_identity,
    ) as endpoint:
        endpoint.app.router.routes[:] = [
            route
            for route in endpoint.app.router.routes
            if getattr(route, "original_router", None) is not assignments.router
        ]
        with wired_sdk(endpoint) as wire, pytest.raises(httpx.HTTPStatusError):
            _sdk(body)
    assert [request.url.path for request in wire.requests] == [PATH]


def test_borrowed_lease_is_rejected_before_http_or_validation(
    client: GatewayTestClient, body: dict[str, Any]
) -> None:
    def never_validated() -> int:
        raise AssertionError("a borrowed lease is rejected before it is validated")

    pin_agent(
        body["actor_agent_id"],
        lease=ExternalLease(body["actor_agent_id"], never_validated, lambda: None),
    )
    with wired_sdk(client) as wire, pytest.raises(ValueError, match="borrowed lease"):
        task_registry.create_and_assign(
            "X",
            "Y",
            parent=body["task"]["parent"],
            operation_key="sdk-compound",
        )
    assert wire.requests == []


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({}, TypeError),
        ({"operation_key": None}, TypeError),
        ({"operation_key": ""}, ValueError),
        ({"operation_key": "x" * 129}, ValueError),
        ({"operation_key": True}, TypeError),
        ({"operation_key": "x", "require_idempotency": True}, TypeError),
        ({"operation_key": "x", "require_idempotency": False}, TypeError),
        ({"operation_key": "x", "preset": "coder"}, TypeError),
    ],
)
def test_missing_invalid_key_and_retired_flag_fail_before_http(
    client: GatewayTestClient,
    body: dict[str, Any],
    kwargs: dict[str, Any],
    error: type[Exception],
) -> None:
    pin_agent(body["actor_agent_id"])
    with wired_sdk(client) as wire, pytest.raises(error):
        task_registry.create_and_assign("X", "Y", parent=body["task"]["parent"], **kwargs)
    assert wire.requests == []


def test_same_principal_key_actor_drift_conflicts(
    client: GatewayTestClient, body: dict[str, Any], db_conn: psycopg.Connection
) -> None:
    with wired_sdk(client):
        original = _sdk(body)
        another = db_conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
        assert another is not None
        db_conn.commit()
        with pytest.raises(httpx.HTTPStatusError) as changed:
            _sdk({**body, "actor_agent_id": another[0]})
        assert changed.value.response.status_code == 409
        assert _sdk(body) == original
    assert db_conn.execute("SELECT count(*) FROM task_assignment_receipts").fetchone() == (1,)


def test_assignment_sends_only_explicit_config(
    client: GatewayTestClient, body: dict[str, Any], db_conn: psycopg.Connection
) -> None:
    with wired_sdk(client) as wire:
        task, agent_id = _sdk(body, config_overlay={"reasoning_effort": "high"})
    assert task.owner == agent_id
    assert json.loads(wire.requests[0].content)["agent"]["config"] == {"reasoning_effort": "high"}
    assert db_conn.execute(
        "SELECT config_overlay FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ({"reasoning_effort": "high"},)
