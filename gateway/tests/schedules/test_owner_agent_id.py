# meiva:note
# A schedule's owner: the agent it speaks as. The API carries the column, an explicit null
# clears it, and the value the list query returns is the same one the row holds.

import psycopg
from fastapi.testclient import TestClient

from base.db import create_agent
from gateway.tests.schedules.api_support import schedule_client


def _create(client: TestClient, name: str, *, owner_agent_id: int | None = None) -> int:
    response = client.post(
        "/api/schedules",
        json={
            "name": name,
            "script": "print(1)\n",
            "command": "python schedule.py",
            "owner_agent_id": owner_agent_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_create_and_update_carry_the_owner_agent_id(db_conn: psycopg.Connection) -> None:
    owner = create_agent(db_conn)
    db_conn.commit()

    with schedule_client() as client:
        sid = _create(client, "owned-schedule", owner_agent_id=owner)
        # meiva:note
        # One expression each, read straight off the response: the list view carries the column
        # too, so an operator scanning the table does not have to open every schedule.
        assert client.get(f"/api/schedules/{sid}").json()["owner_agent_id"] == owner
        assert owner in [row["owner_agent_id"] for row in client.get("/api/schedules").json()]

        updated = client.put(f"/api/schedules/{sid}", json={"owner_agent_id": None})
        assert updated.status_code == 200, updated.text
        assert client.get(f"/api/schedules/{sid}").json()["owner_agent_id"] is None

    assert db_conn.execute(
        "SELECT owner_agent_id FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (None,)


def test_an_absent_owner_leaves_the_column_null(db_conn: psycopg.Connection) -> None:
    with schedule_client() as client:
        sid = _create(client, "plain-schedule")
        assert client.get(f"/api/schedules/{sid}").json()["owner_agent_id"] is None

    assert db_conn.execute(
        "SELECT owner_agent_id FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (None,)
