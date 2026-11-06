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


def test_publication_read_reply_and_overview_independence(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    path = f"/api/agents/{aid}/user-messages"
    body = {"content": "Choose A or B", "response": "required", "suggestions": ["A", "B"]}
    with gateway_test_client() as client:
        first = client.post(path, json=body, headers=headers("decision"))
        assert first.status_code == 201, first.text
        record = UserMessage.model_validate(first.json())
        for i in range(2):
            assert (
                client.post(
                    path,
                    json={"content": f"Milestone {i}", "response": "none", "suggestions": []},
                    headers=headers(f"milestone-{i}"),
                ).status_code
                == 201
            )
        resolve = f"/api/keyed/v1/agents/{aid}/user-messages/{record.id}/resolve"
        assert (
            client.post(resolve, json={"action": "read"}, headers=headers("read")).status_code
            == 200
        )
        pending = UserMessageFeed.model_validate(client.get(path + "?pending=true").json()).items
        question = pending[0]
        assert question.read_at and question.resolved_at is None
        answer = client.post(
            resolve,
            json={"action": "answer", "reply": "B, with a smaller scope"},
            headers=headers("answer"),
        )
        assert answer.status_code == 200, answer.text
        replay = client.post(
            resolve,
            json={"action": "answer", "reply": "B, with a smaller scope"},
            headers=headers("answer"),
        )
        assert replay.json()["inbound_id"] == answer.json()["inbound_id"]
        question = UserMessageFeed.model_validate(client.get(path).json()).items[0]
        assert question.resolution == "answered"
        assert question.reply == "B, with a smaller scope"
        assert len(UserMessageFeed.model_validate(client.get(path).json()).items) == 3
    assert db_conn.execute("SELECT count(*) FROM notification_events").fetchone() == (0,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (aid,)
    ).fetchone() == (1,)


def test_publish_retry_is_atomic_and_rejects_changed_content(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    path = f"/api/agents/{aid}/user-messages"
    body: dict[str, object] = {"content": "Result", "response": "none", "suggestions": []}
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
        assert response.status_code == 200 and response.json()["reason"] == "disabled", (
            response.text
        )
        response = client.post(
            f"/api/agents/{aid}/user-messages",
            json={"content": None, "response": "none", "suggestions": []},
            headers=headers("no-update"),
        )
        assert response.status_code == 201, response.text
        assert client.get(f"/api/agents/{aid}/user-messages").json()["items"] == []
    assert db_conn.execute("SELECT count(*) FROM agent_user_messages").fetchone() == (1,)
