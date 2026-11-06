from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.api_contracts.user_delivery import UserMessage
from gateway.agents.user_delivery.router import UserMessageFeed
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.test_notices_endpoint import seed_agent

pytestmark = pytest.mark.usefixtures("gateway_data_plane")


def headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key, "Idempotency-Scope": "principal-v1"}


def sdk_agent(conn: psycopg.Connection) -> int:
    aid = seed_agent(conn)
    conn.execute(
        "UPDATE agents_meta SET birth_config=%s WHERE id=%s",
        (Jsonb({"user_delivery_mode": "sdk"}), aid),
    )
    conn.commit()
    return aid


def test_publication_read_is_separate_from_ask(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    path = f"/api/agents/{aid}/user-messages"
    with gateway_test_client() as client:
        result = client.post(
            path,
            json={"content": "Milestone", "suggestions": ["Continue"]},
            headers=headers("milestone"),
        )
        assert result.status_code == 201, result.text
        record = UserMessage.model_validate(result.json())
        resolve = f"/api/keyed/v1/agents/{aid}/user-messages/{record.id}/resolve"
        assert (
            client.post(resolve, json={"action": "read"}, headers=headers("read")).status_code
            == 200
        )
        assert client.get(path + "?pending=true").json()["items"] == []
        assert (
            client.post(
                resolve, json={"action": "answer", "reply": "yes"}, headers=headers("answer")
            ).status_code
            == 422
        )
        assert len(UserMessageFeed.model_validate(client.get(path).json()).items) == 1
    assert db_conn.execute("SELECT count(*) FROM notification_events").fetchone() == (0,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (aid,)
    ).fetchone() == (0,)


def test_publish_retry_is_atomic_and_rejects_changed_content(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    path = f"/api/agents/{aid}/user-messages"
    body: dict[str, object] = {"content": "Result", "suggestions": []}
    with gateway_test_client() as client, ThreadPoolExecutor(max_workers=4) as pool:
        calls = [
            pool.submit(client.post, path, json=body, headers=headers("same")) for _ in range(4)
        ]
        responses = [future.result() for future in calls]
        assert all(response.status_code == 201 for response in responses), [
            r.text for r in responses
        ]
        assert len({response.json()["id"] for response in responses}) == 1
        assert (
            client.post(
                path, json=body | {"content": "Different"}, headers=headers("same")
            ).status_code
            == 409
        )
    assert db_conn.execute(
        "SELECT count(*) FROM agent_user_messages WHERE agent_id=%s", (aid,)
    ).fetchone() == (1,)


@pytest.mark.parametrize(
    "body",
    [
        {"content": "Question", "response": "required"},
        {"content": "", "response": "none", "suggestions": []},
        {"content": "Question", "response": "surprise", "suggestions": []},
        {"content": None, "response": "required", "suggestions": []},
        {"content": "Result", "response": "none", "suggestions": ["Approve"]},
    ],
)
def test_bad_publication_is_rejected(db_conn: psycopg.Connection, body: dict[str, object]) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        response = client.post(
            f"/api/agents/{aid}/user-messages", json=body, headers=headers("invalid")
        )
        assert response.status_code == 422
    assert db_conn.execute("SELECT count(*) FROM agent_user_messages").fetchone() == (0,)


def test_sdk_has_one_delivery_entry_and_explicit_silence(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        assert (
            client.post(
                f"/api/agents/{aid}/notices", json={"title": "Duplicate"}, headers=headers("notice")
            ).status_code
            == 409
        )
        response = client.post(f"/api/agents/{aid}/reply-suggestion")
        assert response.status_code == 200 and response.json()["reason"] == "no_agent_text", (
            response.text
        )
        response = client.post(
            f"/api/agents/{aid}/user-messages",
            json={"content": None},
            headers=headers("no-update"),
        )
        assert response.status_code == 201, response.text
        assert client.get(f"/api/agents/{aid}/user-messages").json()["items"] == []
    assert db_conn.execute("SELECT count(*) FROM agent_user_messages").fetchone() == (1,)


def test_ask_retries_note_and_answer_share_one_receipt(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    path = f"/api/agents/{aid}/asks"
    question = {
        "title": "Choose scope",
        "question": {
            "type": "multi_select",
            "options": [{"id": "small", "label": "Small"}],
            "note_suggestion": "Validate first",
        },
    }
    with gateway_test_client() as client:
        created = client.post(path, json=question, headers=headers("question"))
        assert created.status_code == 201, created.text
        question_id = created.json()["id"]
        row = client.get(f"/api/notices/open?agent_id={aid}").json()[0]
        assert row["answer"] is None and row["expire_at"] is None
        assert row["question"]["note_suggestion"] == "Validate first"
        resolve = f"/api/keyed/v1/agents/{aid}/notices/{question_id}/resolve"
        body = {
            "action": "answer",
            "answer": {"option_ids": ["small"], "comment": "Validate tonight"},
        }
        answer = client.post(resolve, json=body, headers=headers("answer"))
        assert answer.status_code == 201, answer.text
        assert client.post(resolve, json=body, headers=headers("answer")).json() == answer.json()
        assert client.get(f"/api/notices/open?agent_id={aid}").json() == []
    assert db_conn.execute("SELECT count(*) FROM notification_events").fetchone() == (0,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (aid,)
    ).fetchone() == (1,)
    row = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE agent_id=%s", (aid,)
    ).fetchone()
    assert row is not None and "Validate tonight" in row[0]


def test_feedback_requires_an_offered_candidate_and_records_explicit_intent(
    db_conn: psycopg.Connection,
) -> None:
    from gateway.agents.reply_suggestion.feedback import save_offer

    aid = sdk_agent(db_conn)
    offer = save_offer(db_conn, aid, "Release", ["Ship", "Wait"])
    db_conn.commit()
    path = f"/api/agents/{aid}/reply-suggestion/feedback"
    body = {
        "offer_id": offer,
        "candidate_index": 0,
        "outcome": "edited",
        "sent_text": "Ship to staging",
    }
    with gateway_test_client() as client:
        assert client.post(path, json=body, headers=headers("feedback")).status_code == 200
        assert client.post(path, json=body, headers=headers("feedback")).status_code == 200
        assert (
            client.post(
                path, json=body | {"outcome": "accepted"}, headers=headers("invalid")
            ).status_code
            == 422
        )
        assert (
            client.post(
                path, json=body | {"candidate_index": 9}, headers=headers("missing")
            ).status_code
            == 422
        )
        assert (
            client.post(
                path,
                json={"offer_id": offer, "candidate_index": 1, "outcome": "rejected"},
                headers=headers("reject"),
            ).status_code
            == 200
        )
    assert db_conn.execute(
        "SELECT outcome,sent_text FROM reply_suggestion_feedback ORDER BY id"
    ).fetchall() == [("edited", "Ship to staging"), ("rejected", None)]


def test_ask_creation_retry_preserves_each_independent_question(
    db_conn: psycopg.Connection,
) -> None:
    aid = sdk_agent(db_conn)
    path = f"/api/agents/{aid}/asks"
    question = {"title": "Choose scope", "question": {"type": "text"}}
    with gateway_test_client() as client, ThreadPoolExecutor(max_workers=3) as pool:
        calls = [
            pool.submit(client.post, path, json=question, headers=headers("question"))
            for _ in range(3)
        ]
        results = [call.result() for call in calls]
        assert all(result.status_code == 201 for result in results), results
        assert len({result.json()["id"] for result in results}) == 1
        changed = question | {"title": "Choose timing"}
        assert client.post(path, json=changed, headers=headers("question")).status_code == 409
        assert client.post(path, json=changed, headers=headers("timing")).status_code == 201
        assert len(client.get(f"/api/notices/open?agent_id={aid}").json()) == 2
