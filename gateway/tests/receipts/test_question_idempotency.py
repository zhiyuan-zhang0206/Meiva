from concurrent.futures import ThreadPoolExecutor

import psycopg

from base.events.live.bus import EventBus
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.test_questions_endpoint import (
    answer_body,
    answers_path,
    insert_question,
    seed_agent,
)
from gateway.tests.support.live_events.faults import unpublishable_bus


def test_concurrent_resolution_replays_one_reply(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    question = insert_question(db_conn, agent, "question")
    path = answers_path(agent)
    with gateway_test_client() as client, ThreadPoolExecutor(max_workers=4) as executor:

        def resolve() -> dict[str, object]:
            response = client.post(
                path,
                json=answer_body(question, "yes"),
                headers={"Idempotency-Key": "answer-once", "Idempotency-Scope": "principal-v1"},
            )
            assert response.status_code == 201
            return response.json()

        results = [future.result() for future in [executor.submit(resolve) for _ in range(4)]]
        assert len({result["inbound_id"] for result in results}) == 1
        changed = client.post(
            path,
            json=answer_body(question, "no"),
            headers={"Idempotency-Key": "answer-once", "Idempotency-Scope": "principal-v1"},
        )
        assert changed.status_code == 409
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent,))
        assert cur.fetchone() == (1,)


def test_later_answer_requires_a_new_open_question(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    question = insert_question(db_conn, agent, "Question")
    path = answers_path(agent)
    with gateway_test_client() as client:
        body = answer_body(question, "yes")
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


def test_resolution_recovers_after_committed_wake_failure(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    agent = seed_agent(db_conn)
    question = insert_question(db_conn, agent, "question")
    path = answers_path(agent)

    with gateway_test_client(raise_server_exceptions=False) as client:
        client.app.state.bus = unpublishable_bus()
        first = client.post(
            path,
            json=answer_body(question, "yes"),
            headers={"Idempotency-Key": "lost-tail", "Idempotency-Scope": "principal-v1"},
        )
        assert first.status_code == 500
        client.app.state.bus = event_bus
        second = client.post(
            path,
            json=answer_body(question, "yes"),
            headers={"Idempotency-Key": "lost-tail", "Idempotency-Scope": "principal-v1"},
        )
        assert second.status_code == 201
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM inbound_messages WHERE agent_id=%s", (agent,))
        assert cur.fetchall() == [(second.json()["inbound_id"],)]


def test_missing_key_cannot_mutate_question(db_conn: psycopg.Connection) -> None:
    agent = seed_agent(db_conn)
    question = insert_question(db_conn, agent, "original")
    path = answers_path(agent)
    with gateway_test_client() as client:
        assert client.post(path, json=answer_body(question, "yes")).status_code == 422
    assert db_conn.execute(
        "SELECT title, resolved_at FROM agent_questions WHERE agent_id=%s", (agent,)
    ).fetchall() == [("original", None)]
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM keyed_operation_receipts").fetchone() == (0,)
