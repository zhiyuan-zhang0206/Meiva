# meiva:note
# A schedule's owner is a provisioning fact, not an API field.
#
# Whoever a schedule speaks as decides whose name its unattended writes carry, and the gateway
# cannot tell its callers apart (one shared administrator credential, caller labels deliberately
# absent). An API-exposed owner field therefore lets any caller hand a schedule another agent's
# name. Both request shapes refuse the field with the sentence that names the operator path; a
# created schedule starts NULL; the read view still carries what provisioning set; and the operator
# tool writes the row and its audit entry together.

import httpx2
import psycopg
from fastapi.testclient import TestClient

from base.db import create_agent
from gateway.tests.schedules.api_support import schedule_client

_REFUSAL = "owner is assigned through provisioning, not the API"
_SCRIPT = "print(1)\n"


def _post(client: TestClient, name: str, **extra: object) -> httpx2.Response:
    return client.post(
        "/api/schedules",
        json={"name": name, "script": _SCRIPT, "command": "python schedule.py", **extra},
    )


def _create(client: TestClient, name: str) -> int:
    response = _post(client, name)
    assert response.status_code == 201, response.text
    return response.json()["id"]


# meiva:note
# Any supplied value is refused, an explicit null included: "clear the owner" is exactly as much an
# assignment as naming one, and the operator path is the only place either happens.
def test_create_refuses_an_owner_however_it_is_written(db_conn: psycopg.Connection) -> None:
    owner = create_agent(db_conn)
    db_conn.commit()

    with schedule_client() as client:
        for supplied in (owner, None):
            response = _post(client, f"refused-{supplied}", owner_agent_id=supplied)
            assert response.status_code == 422, response.text
            assert _REFUSAL in response.text


def test_update_refuses_the_field(db_conn: psycopg.Connection) -> None:
    owner = create_agent(db_conn)
    db_conn.commit()

    with schedule_client() as client:
        sid = _create(client, "unowned-schedule")
        for supplied in (owner, None):
            response = client.put(f"/api/schedules/{sid}", json={"owner_agent_id": supplied})
            assert response.status_code == 422, response.text
            assert _REFUSAL in response.text


def test_a_created_schedule_starts_without_an_owner(db_conn: psycopg.Connection) -> None:
    with schedule_client() as client:
        sid = _create(client, "plain-schedule")
        assert client.get(f"/api/schedules/{sid}").json()["owner_agent_id"] is None

    assert db_conn.execute(
        "SELECT owner_agent_id FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (None,)
