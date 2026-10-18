from __future__ import annotations

import psycopg

from tests.fixtures.gateway_config import gateway_test_client


def _agent(conn: psycopg.Connection) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    conn.execute(
        "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'idling')",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def test_categories_report_the_core_set_with_switches(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/notifications/categories")
    assert resp.status_code == 200
    categories = {item["id"]: item for item in resp.json()["categories"]}
    assert {"core.turn_finished", "core.needs_input", "core.notice"} <= set(categories)
    assert categories["core.notice"]["enabled"] is False
    assert categories["core.turn_finished"]["enabled"] is True


def test_an_undeclared_category_is_rejected(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{agent_id}/notifications",
            json={"category": "nobody.nothing", "subject": "hello"},
        )
    assert resp.status_code == 422


def test_a_delivery_is_recorded_and_deduplicated(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    payload = {
        "category": "core.needs_input",
        "subject": "Agent is waiting on your answer",
        "dedupe_key": "notice:test",
    }
    with gateway_test_client() as client:
        first = client.post(f"/api/agents/{agent_id}/notifications", json=payload)
        second = client.post(f"/api/agents/{agent_id}/notifications", json=payload)
    assert first.status_code == 201
    assert first.json()["delivered"] is True
    assert second.json() == {"delivered": False, "id": None}
    rows = db_conn.execute(
        "SELECT category, subject FROM notification_events WHERE agent_id = %s", (agent_id,)
    ).fetchall()
    assert rows == [("core.needs_input", "Agent is waiting on your answer")]


def test_an_empty_subject_is_rejected(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{agent_id}/notifications",
            json={"category": "core.turn_finished", "subject": "   "},
        )
    assert resp.status_code == 422
