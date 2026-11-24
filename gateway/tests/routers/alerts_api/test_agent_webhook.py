import psycopg
from psycopg_pool import ConnectionPool

from base.db import create_agent
from gateway.tests.routers.alerts_api.alerts_support import (
    alert,
    alerts_client,
    post_webhook,
    webhook,
)

_AUTH = {"Authorization": "Bearer test-token"}


def _agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute("INSERT INTO agents_meta (id, status) VALUES (%s, 'idling')", (agent_id,))
    conn.commit()
    return agent_id


def test_the_alert_webhook_does_not_wake_an_agent(db_conn: psycopg.Connection) -> None:
    _agent(db_conn)
    with alerts_client() as client:
        assert post_webhook(client, webhook()).status_code == 204
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)


def test_one_inbox_message_for_a_mixed_group(db_conn: psycopg.Connection) -> None:
    recipient = _agent(db_conn)
    _agent(db_conn)
    first = alert(severity="error", summary="CPU saturated")
    first["labels"].update({"agent_id": "77", "machine": "runner-a", "event_name": "host_probe"})
    second = alert(
        status="resolved", severity="critical", summary="Disk recovered", fingerprint="b"
    )
    with alerts_client(credentials=False) as client:
        response = client.post(
            f"/api/alerts/agents/{recipient}", json=webhook(alerts=[first, second]), headers=_AUTH
        )
    assert response.status_code == 204, response.text

    rows = db_conn.execute(
        "SELECT agent_id, source, source_verified_by, content FROM inbound_messages"
    ).fetchall()
    assert len(rows) == 1
    agent_id, source, verified_by, content = rows[0]
    assert (agent_id, source, verified_by) == (recipient, "system:alertmanager", "webhook:alerts")
    assert "[firing] [error] test-rule" in content
    assert "[resolved] [critical] test-rule" in content
    assert "CPU saturated" in content and "Disk recovered" in content
    assert "agent_id=77, machine=runner-a" in content
    assert "meiva events ls --from 2026-08-04T10:00:00+00:00 --to " in content
    assert "--agent=77 --machine=runner-a --name=host_probe" in content
    assert "http://localhost:3002/alerting/xyz/edit" in content


def test_resolved_group_uses_alertmanagers_status_and_repeats_are_delivered(
    db_conn: psycopg.Connection,
) -> None:
    recipient = _agent(db_conn)
    instance = alert()
    del instance["status"]
    body = webhook(status="resolved", alerts=[instance])
    with alerts_client(credentials=False) as client:
        for _ in range(2):
            assert (
                client.post(f"/api/alerts/agents/{recipient}", json=body, headers=_AUTH).status_code
                == 204
            )

    rows = db_conn.execute("SELECT content FROM inbound_messages").fetchall()
    assert len(rows) == 2
    assert all("[resolved] [error] test-rule" in row[0] for row in rows)


def test_authentication_failure_leaves_inbox_empty(db_conn: psycopg.Connection) -> None:
    recipient = _agent(db_conn)
    path = f"/api/alerts/agents/{recipient}"
    with alerts_client(credentials=False, client=("127.0.0.1", 5000)) as client:
        assert client.post(path, json=webhook()).status_code == 401
        assert client.get(path, headers=_AUTH).json()["code"] == "authentication_required"
        assert (
            client.post(f"{path}/extra", json=webhook()).json()["code"] == "authentication_required"
        )
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)


def test_enqueue_failure_is_non_success(db_conn: psycopg.Connection) -> None:
    recipient = _agent(db_conn)
    with alerts_client(credentials=False, raise_server_exceptions=False) as client:
        pool = client.app.state.db_pool
        assert isinstance(pool, ConnectionPool)
        pool.close()
        response = client.post(f"/api/alerts/agents/{recipient}", json=webhook(), headers=_AUTH)
    assert response.is_server_error, response.text
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
