from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from base.api_contracts.idempotency import keyed_headers
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


def test_cluster_schedule_records_creator_and_preserves_cluster_placement(
    db_conn: psycopg.Connection,
) -> None:
    creator = _owner(db_conn, "creator-machine")
    with schedule_client() as client:
        owned_resp = _post(
            client, "agent-owned", owner_agent_id=creator, created_by_agent_id=creator
        )
        cluster_resp = _post(
            client,
            "cluster-created-by-agent",
            owner_agent_id=None,
            created_by_agent_id=creator,
        )
        assert owned_resp.status_code == 201, owned_resp.text
        assert cluster_resp.status_code == 201, cluster_resp.text
        cluster_id = cluster_resp.json()["id"]

        assert (owned_resp.json()["owner_agent_id"], owned_resp.json()["created_by_agent_id"]) == (
            creator,
            creator,
        )
        assert (
            cluster_resp.json()["owner_agent_id"],
            cluster_resp.json()["created_by_agent_id"],
            cluster_resp.json()["on_owner_terminate"],
        ) == (None, creator, "keep")
        assert cluster_resp.json()["machine"] != "creator-machine"

        listed = {row["id"]: row for row in client.get("/api/schedules").json()}
        assert (
            listed[cluster_id]["owner_agent_id"],
            listed[cluster_id]["created_by_agent_id"],
        ) == (
            None,
            creator,
        )
        assert client.get(f"/api/schedules/{cluster_id}").json()["created_by_agent_id"] == creator


def test_terminating_creator_cancels_owned_schedule_but_keeps_cluster_schedule(
    db_conn: psycopg.Connection,
) -> None:
    creator = _owner(db_conn, "creator-machine")
    with schedule_client() as client:
        owned_id = _post(
            client, "agent-owned", owner_agent_id=creator, created_by_agent_id=creator
        ).json()["id"]
        cluster_id = _post(
            client,
            "cluster-created-by-agent",
            owner_agent_id=None,
            created_by_agent_id=creator,
        ).json()["id"]

    db_conn.execute(
        "UPDATE agents_meta SET status = 'terminated' WHERE id = %s",
        (creator,),
    )
    db_conn.commit()

    owned_state = db_conn.execute(
        "SELECT enabled, status FROM schedules WHERE id = %s",
        (owned_id,),
    ).fetchone()
    cluster_state = db_conn.execute(
        "SELECT enabled, status, owner_agent_id, created_by_agent_id FROM schedules WHERE id = %s",
        (cluster_id,),
    ).fetchone()
    assert owned_state == (False, "cancelled")
    assert cluster_state == (True, "scheduled", None, creator)


def test_missing_created_by_agent_is_rejected(db_conn: psycopg.Connection) -> None:
    with schedule_client() as client:
        response = _post(client, "missing-creator", created_by_agent_id=999_999)

    assert response.status_code == 400
    assert response.json()["detail"] == "created_by_agent_id does not exist"


def test_idempotent_create_replay_preserves_created_by_agent_id(
    db_conn: psycopg.Connection,
) -> None:
    creator = _owner(db_conn)
    headers = keyed_headers("create-with-provenance")
    with schedule_client() as client:
        first = client.post(
            "/api/schedules",
            json={
                "name": "replay-cluster",
                "trigger_kind": "every",
                "interval_seconds": 60,
                "script": "print(1)\n",
                "created_by_agent_id": creator,
            },
            headers=headers,
        )
        replay = client.post(
            "/api/schedules",
            json={
                "name": "replay-cluster",
                "trigger_kind": "every",
                "interval_seconds": 60,
                "script": "print(1)\n",
                "created_by_agent_id": creator,
            },
            headers=headers,
        )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["owner_agent_id"] is None
    assert replay.json()["created_by_agent_id"] == creator
