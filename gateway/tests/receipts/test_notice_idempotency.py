from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from base.api_contracts.idempotency import keyed_headers
from base.events.live.bus import EventBus
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.test_notices_endpoint import insert_notice, seed_agent
from gateway.tests.support.live_events.faults import unpublishable_bus


def test_concurrent_resolution_replays_one_reply(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    notice = insert_notice(db_conn, agent, "question", require_response=True)
    path = f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve"
    with gateway_test_client() as client, ThreadPoolExecutor(max_workers=4) as executor:

        def resolve() -> dict[str, object]:
            response = client.post(
                path,
                json={"action": "answer", "reply": "yes"},
                headers={"Idempotency-Key": "answer-once", "Idempotency-Scope": "principal-v1"},
            )
            assert response.status_code == 201
            return response.json()

        results = [future.result() for future in [executor.submit(resolve) for _ in range(4)]]
        assert len({result["inbound_id"] for result in results}) == 1
        changed = client.post(
            path,
            json={"action": "answer", "reply": "no"},
            headers={"Idempotency-Key": "answer-once", "Idempotency-Scope": "principal-v1"},
        )
        assert changed.status_code == 409
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent,))
        assert cur.fetchone() == (1,)


def test_later_answer_requires_a_new_open_question(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    notice = insert_notice(db_conn, agent, "Question", require_response=True)
    path = f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve"
    with gateway_test_client() as client:
        body = {"action": "answer", "reply": "yes"}
        first = client.post(
            path,
            json=body,
            headers={"Idempotency-Key": "read-1", "Idempotency-Scope": "principal-v1"},
        )
        replay = client.post(
            path,
            json=body,
            headers={"Idempotency-Key": "read-1", "Idempotency-Scope": "principal-v1"},
        )
        later = client.post(
            path,
            json=body,
            headers={"Idempotency-Key": "read-2", "Idempotency-Scope": "principal-v1"},
        )
    assert first.status_code == replay.status_code == 201
    assert later.status_code == 409
    assert first.json()["inbound_id"] == replay.json()["inbound_id"]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)


def test_creation_replay_survives_a_closed_notice(
    db_conn: psycopg.Connection,
) -> None:
    # meiva:note
    # A replay answers with the original acceptance even once that notice is closed:
    # receipt lookup precedes the mutable target checks. Closing is the only way past
    # an open notice (user ruling 2026-10-21), so the replay is checked against a
    # dismissed row.
    # meiva:link gateway/agents/docs/notice-receipts.meiva.okf.md
    agent = seed_agent(db_conn)
    path = f"/api/agents/{agent}/notices"
    with gateway_test_client() as client:
        body = {"title": "original"}
        first = client.post(path, json=body, headers=keyed_headers("create-1"))
        with db_conn.cursor() as cur:
            cur.execute("SELECT id FROM agent_notices WHERE agent_id = %s", (agent,))
            row = cur.fetchone()
        assert row is not None
        global_id = row[0]
        closed = client.post(
            f"/api/keyed/v1/agents/{agent}/notices/{global_id}/resolve",
            json={"action": "dismiss"},
            headers={"Idempotency-Key": "dismiss-original", "Idempotency-Scope": "principal-v1"},
        )
        later = client.post(path, json={"title": "later"}, headers=keyed_headers("create-2"))
        replay = client.post(path, json=body, headers=keyed_headers("create-1"))
        conflict = client.post(path, json={"title": "different"}, headers=keyed_headers("create-1"))
    assert first.status_code == closed.status_code == later.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT title, resolution FROM agent_notices WHERE agent_id=%s ORDER BY id", (agent,)
        )
        assert cur.fetchall() == [("original", "dismissed"), ("later", None)]


def test_creation_key_requires_the_principal_scope(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    with gateway_test_client() as client:
        response = client.post(
            f"/api/agents/{agent}/notices",
            json={"title": "unscoped"},
            headers={"Idempotency-Key": "unscoped-create"},
        )
    assert response.status_code == 422
    assert "Idempotency-Scope" in response.text
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_notices WHERE agent_id=%s", (agent,))
        assert cur.fetchone() == (0,)


def test_resolution_recovers_after_committed_wake_failure(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    agent = seed_agent(db_conn)
    notice = insert_notice(db_conn, agent, "question", require_response=True)
    path = f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve"

    with gateway_test_client(raise_server_exceptions=False) as client:
        client.app.state.bus = unpublishable_bus()
        first = client.post(
            path,
            json={"action": "answer", "reply": "yes"},
            headers={"Idempotency-Key": "lost-tail", "Idempotency-Scope": "principal-v1"},
        )
        assert first.status_code == 500
        client.app.state.bus = event_bus
        second = client.post(
            path,
            json={"action": "answer", "reply": "yes"},
            headers={"Idempotency-Key": "lost-tail", "Idempotency-Scope": "principal-v1"},
        )
        assert second.status_code == 201
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM inbound_messages WHERE agent_id=%s", (agent,))
        assert cur.fetchall() == [(second.json()["inbound_id"],)]


def test_create_receipt_survives_expiration(
    db_conn: psycopg.Connection,
) -> None:
    # meiva:note
    # A replay answers from the receipt before any mutable precondition is checked: the task
    # the first request named is gone when it is retried, and a fresh request naming it
    # would be refused.
    agent = seed_agent(db_conn)
    path = f"/api/agents/{agent}/notices"
    task = db_conn.execute(
        "INSERT INTO agent_tasks (title, description, status, created_by) "
        "VALUES ('t', '', 'in_progress', 'user') RETURNING id"
    ).fetchone()
    assert task is not None
    db_conn.commit()
    with gateway_test_client() as client:
        body = {"title": "expiring", "task_id": task[0]}
        first = client.post(path, json=body, headers=keyed_headers("expires"))
        db_conn.execute("UPDATE agent_notices SET task_id = NULL WHERE agent_id = %s", (agent,))
        db_conn.execute("DELETE FROM agent_tasks WHERE id = %s", (task[0],))
        db_conn.commit()
        fresh = client.post(path, json=body, headers=keyed_headers("fresh"))
        replay = client.post(path, json=body, headers=keyed_headers("expires"))
    assert first.status_code == replay.status_code == 201
    assert fresh.status_code == 422
    assert first.json() == replay.json()


@pytest.mark.parametrize("resolve", [False, True])
def test_missing_key_cannot_mutate_notice(db_conn: psycopg.Connection, resolve: bool) -> None:
    agent = seed_agent(db_conn)
    notice = insert_notice(db_conn, agent, "original", require_response=True)
    path = f"/api/agents/{agent}/notices"
    body = {"title": "replacement"}
    if resolve:
        path = f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve"
        body = {"action": "answer", "reply": "yes"}
    with gateway_test_client() as client:
        assert client.post(path, json=body).status_code == 422
    assert db_conn.execute(
        "SELECT title, resolved_at FROM agent_notices WHERE agent_id=%s", (agent,)
    ).fetchall() == [("original", None)]
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (0,)
