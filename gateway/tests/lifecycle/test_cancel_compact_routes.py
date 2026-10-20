# meiva:note
# `POST /api/agents/{id}/cancel` forwards to the home runner; `/compact` queues one request.

from __future__ import annotations

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from tests.fixtures.gateway_config import gateway_test_client, spawn_via_http


@pytest.fixture
def _local_machine(set_machine_identity) -> MachineIdentity:
    return set_machine_identity(role="agent-runner", name="local-test")


def _set_agent(db_conn: psycopg.Connection, agent_id: int, *, machine: str, status: str) -> None:
    db_conn.execute(
        "UPDATE agents_meta SET machine = %s, status = %s WHERE id = %s",
        (machine, status, agent_id),
    )
    db_conn.commit()


def test_cancel_of_an_unknown_agent_is_404(_local_machine: MachineIdentity) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        assert client.post("/api/agents/987654/cancel").status_code == 404


def test_compact_queues_one_request_for_the_agents_next_turn(
    _local_machine: MachineIdentity, db_conn: psycopg.Connection
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        agent_id = spawn_via_http(client, {}).json()["id"]
        _set_agent(db_conn, agent_id, machine="local-test", status="idling")
        response = client.post(f"/api/agents/{agent_id}/compact")
    assert response.status_code == 200
    inbound_id = response.json()["inbound_id"]
    assert db_conn.execute(
        "SELECT agent_id,kind,status FROM inbound_messages WHERE id = %s", (inbound_id,)
    ).fetchone() == (agent_id, "compact_request", "pending")


def test_compact_refuses_a_terminated_agent_and_queues_nothing(
    _local_machine: MachineIdentity, db_conn: psycopg.Connection
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        agent_id = spawn_via_http(client, {}).json()["id"]
        _set_agent(db_conn, agent_id, machine="local-test", status="terminated")
        response = client.post(f"/api/agents/{agent_id}/compact")
    assert response.status_code == 409
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'compact_request'",
        (agent_id,),
    ).fetchone() == (0,)


def test_compact_of_an_unknown_agent_is_404(_local_machine: MachineIdentity) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        assert client.post("/api/agents/987654/compact").status_code == 404
