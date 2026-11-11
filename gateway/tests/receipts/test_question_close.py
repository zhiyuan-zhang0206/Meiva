import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.test_questions_endpoint import seed_agent
from gateway.tests.receipts.test_user_delivery import headers, sdk_agent

pytestmark = pytest.mark.usefixtures("gateway_data_plane")


def ask(client: TestClient, aid: int, key: str, title: str) -> int:
    created = client.post(
        f"/api/agents/{aid}/asks",
        json={"questions": [{"title": title, "question": {"type": "text"}}]},
        headers=headers(key),
    )
    assert created.status_code == 201, created.text
    return int(created.json()["ids"][0])


def pushes(conn: psycopg.Connection, aid: int) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT action, subject FROM notification_events "
            "WHERE agent_id=%s AND category='core.question' ORDER BY id",
            (aid,),
        ).fetchall()
    ]


def test_closing_an_open_question_withdraws_it_and_its_push(db_conn: psycopg.Connection) -> None:
    aid = sdk_agent(db_conn)
    with gateway_test_client() as client:
        first = ask(client, aid, "a", "First?")
        second = ask(client, aid, "b", "Second?")
        assert first != second
        closed = client.post(f"/api/agents/{aid}/asks/{first}/close", headers=headers("close"))
        assert closed.status_code == 200, closed.text
        assert closed.json() == {"id": first}
        assert [row["id"] for row in client.get(f"/api/questions/open?agent_id={aid}").json()] == [
            second
        ]
        assert client.post(
            f"/api/agents/{aid}/asks/{first}/close", headers=headers("close")
        ).json() == {"id": first}
        assert (
            client.post(
                f"/api/agents/{aid}/asks/{first}/close", headers=headers("again")
            ).status_code
            == 200
        )
    assert db_conn.execute(
        "SELECT resolution, reply, answer FROM agent_questions WHERE id=%s", (first,)
    ).fetchone() == ("withdrawn", None, None)
    assert pushes(db_conn, aid) == [
        ("alert", "First?"),
        ("replace", "2 questions awaiting an answer"),
        ("replace", "Second?"),
    ]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (aid,)
    ).fetchone() == (0,)


def test_closing_refuses_a_missing_foreign_or_already_resolved_question(
    db_conn: psycopg.Connection,
) -> None:
    aid = sdk_agent(db_conn)
    other = seed_agent(db_conn)
    with gateway_test_client() as client:
        answered = ask(client, aid, "answered", "Answered?")
        dismissed = ask(client, aid, "dismissed", "Dismissed?")
        resolved = client.post(
            f"/api/keyed/v1/agents/{aid}/questions/answers",
            json={
                "expected_ids": [answered, dismissed],
                "results": [{"id": answered, "reply": "yes"}, {"id": dismissed, "skip": True}],
            },
            headers=headers("resolve"),
        )
        assert resolved.status_code == 201, resolved.text
        mine = ask(client, aid, "mine", "Mine?")

        def close(agent: int, qid: int, key: str) -> int:
            return client.post(
                f"/api/agents/{agent}/asks/{qid}/close", headers=headers(key)
            ).status_code

        assert close(aid, 999999, "k1") == 404
        assert close(other, mine, "k2") == 404
        assert close(aid, answered, "k3") == 409
        assert close(aid, dismissed, "k4") == 409
        assert [row["id"] for row in client.get(f"/api/questions/open?agent_id={aid}").json()] == [
            mine
        ]
    assert db_conn.execute(
        "SELECT id, resolution FROM agent_questions WHERE agent_id=%s ORDER BY id", (aid,)
    ).fetchall() == [(answered, "answered"), (dismissed, "dismissed"), (mine, None)]
