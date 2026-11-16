from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from base.db import create_agent
from gateway.tests.schedules.api_support import schedule_client


def _owner(db_conn: psycopg.Connection, machine: str = "owner-machine") -> int:
    owner = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', %s)",
        (owner, machine),
    )
    db_conn.commit()
    return owner


def _post(client: TestClient, name: str, **extra: object):
    return client.post(
        "/api/schedules",
        json={
            "name": name,
            "trigger_kind": "every",
            "interval_seconds": 60,
            "script": "print(1)\n",
            **extra,
        },
    )


def test_create_accepts_owner_and_defaults_to_its_machine(
    db_conn: psycopg.Connection,
) -> None:
    owner = _owner(db_conn)
    with schedule_client() as client:
        response = _post(client, "owned", owner_agent_id=owner)

    assert response.status_code == 201, response.text
    assert response.json()["owner_agent_id"] == owner
    assert response.json()["machine"] == "owner-machine"
    assert response.json()["on_owner_terminate"] == "cancel"


def test_system_schedule_defaults_to_keep(db_conn: psycopg.Connection) -> None:
    with schedule_client() as client:
        response = _post(client, "system")

    assert response.status_code == 201, response.text
    assert response.json()["owner_agent_id"] is None
    assert response.json()["on_owner_terminate"] == "keep"


def test_update_owner_moves_schedule_to_owner_machine(db_conn: psycopg.Connection) -> None:
    owner = _owner(db_conn, "new-owner-machine")
    with schedule_client() as client:
        sid = _post(client, "adopted").json()["id"]
        response = client.put(f"/api/schedules/{sid}", json={"owner_agent_id": owner})

    assert response.status_code == 200, response.text
    assert response.json()["owner_agent_id"] == owner
    assert response.json()["machine"] == "new-owner-machine"
    assert response.json()["on_owner_terminate"] == "cancel"


def test_missing_owner_is_rejected(db_conn: psycopg.Connection) -> None:
    with schedule_client() as client:
        response = _post(client, "missing-owner", owner_agent_id=999_999)

    assert response.status_code == 400
    assert "not found" in response.json()["detail"]


def test_message_payload_targets_owner(db_conn: psycopg.Connection) -> None:
    owner = _owner(db_conn)
    with schedule_client() as client:
        response = client.post(
            "/api/schedules",
            json={
                "name": "wake-owner",
                "trigger_kind": "every",
                "interval_seconds": 60,
                "message": "wake up",
                "owner_agent_id": owner,
            },
        )

    assert response.status_code == 201, response.text
    assert f"meiva.agents.send_message({owner}, 'wake up')" in response.json()["script"]
