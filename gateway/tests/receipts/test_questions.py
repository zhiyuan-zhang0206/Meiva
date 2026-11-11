from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.api_contracts.idempotency import keyed_headers
from gateway.tests.events.test_questions_endpoint import seed_agent
from gateway.tests.receipts.test_guarded_question_resolution import client as client

QUESTION = {
    "type": "multi_select",
    "options": [
        {"id": "web", "label": "Web", "description": None},
        {"id": "desktop", "label": "Desktop", "description": None},
    ],
    "allow_other": False,
}


def post_question(client: TestClient, agent: int, question: dict[str, object] | None = None) -> int:
    response = client.post(
        f"/api/agents/{agent}/asks",
        json={
            "title": "Which surfaces?",
            "content": "Choose the scope.",
            "question": QUESTION if question is None else question,
        },
        headers=keyed_headers(str(uuid4())),
    )
    assert response.status_code == 201, response.text
    feed = client.get("/api/questions").json()
    row = next(n for n in feed["awaiting"] if n["agent_id"] == agent)
    assert row["question"] == (QUESTION if question is None else question)
    return row["id"]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "answer", "answer": {"option_ids": ["desktop"], "comment": "Use 1 later."}},
        {"action": "answer", "reply": "2\nUse 1 later."},
    ],
)
def test_answer_round_trip_and_retry_deliver_one_numbered_inbound(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, object]
) -> None:
    agent = seed_agent(db_conn)
    question = post_question(client, agent)
    path = f"/api/keyed/v1/agents/{agent}/questions/{question}/resolve"
    headers = keyed_headers(str(uuid4()))
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 201, first.text
    assert client.post(path, json=body, headers=headers).json() == first.json()
    row = db_conn.execute(
        "SELECT resolution,answer,reply FROM agent_questions WHERE id=%s", (question,)
    ).fetchone()
    assert row is not None
    assert row[0] == "answered"
    assert row[1]["option_ids"] == ["desktop"]
    assert row[1]["comment"] == "Use 1 later."
    assert "1. Web\n2. Desktop" in row[2]
    assert "Selected: 2" in row[2]
    inbound = db_conn.execute(
        "SELECT source,content FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchall()
    assert len(inbound) == 1
    assert inbound[0][0] == "system:question-reply"
    assert "Comment: Use 1 later." in inbound[0][1]
    assert client.get("/api/questions").json()["awaiting"] == []


@pytest.mark.parametrize(
    "answer", [{}, {"option_ids": ["missing"]}, {"text": "yes"}, {"other_text": " "}]
)
def test_invalid_answer_has_no_resolution_or_inbound(
    client: TestClient, db_conn: psycopg.Connection, answer: dict[str, object]
) -> None:
    agent = seed_agent(db_conn)
    question = post_question(client, agent)
    response = client.post(
        f"/api/keyed/v1/agents/{agent}/questions/{question}/resolve",
        json={"action": "answer", "answer": answer},
        headers=keyed_headers(str(uuid4())),
    )
    assert response.status_code == 422, response.text
    assert db_conn.execute(
        "SELECT resolved_at,answer FROM agent_questions WHERE id=%s", (question,)
    ).fetchone() == (None, None)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


def test_open_question_and_dismissal_have_distinct_effects(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    question = post_question(client, agent, {"type": "text"})
    response = client.post(
        f"/api/keyed/v1/agents/{agent}/questions/{question}/resolve",
        json={"action": "answer", "answer": {"text": "Keep it readable."}},
        headers=keyed_headers(str(uuid4())),
    )
    assert response.status_code == 201, response.text
    later = post_question(client, agent)
    dismissed = client.post(
        f"/api/keyed/v1/agents/{agent}/questions/{later}/resolve",
        json={"action": "dismiss"},
        headers=keyed_headers(str(uuid4())),
    )
    assert dismissed.status_code == 201
    assert db_conn.execute(
        "SELECT resolution,answer,reply FROM agent_questions WHERE id=%s", (later,)
    ).fetchone() == ("dismissed", None, None)


def test_ask_requires_a_response_and_cannot_be_marked_read(
    client: TestClient,
    db_conn: psycopg.Connection,
) -> None:
    agent = seed_agent(db_conn)
    response = client.post(
        f"/api/agents/{agent}/asks",
        json={"title": "Which scope?", "question": {"type": "text"}},
        headers=keyed_headers(str(uuid4())),
    )
    assert response.status_code == 201, response.text
    question = next(n for n in client.get("/api/questions/open").json() if n["agent_id"] == agent)
    rejected = client.post(
        f"/api/keyed/v1/agents/{agent}/questions/{question['id']}/resolve",
        json={"action": "read"},
        headers=keyed_headers(str(uuid4())),
    )
    assert rejected.status_code == 422
    assert db_conn.execute(
        "SELECT resolved_at FROM agent_questions WHERE id=%s", (question["id"],)
    ).fetchone() == (None,)


def question_notifications(
    conn: psycopg.Connection, agent: int
) -> list[tuple[str, str, str, str | None]]:
    rows = conn.execute(
        "SELECT action, tag, subject, body FROM notification_events "
        "WHERE agent_id=%s AND category='core.question' ORDER BY id",
        (agent,),
    ).fetchall()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "answer", "answer": {"option_ids": ["desktop"], "comment": "Use 1 later."}},
        {"action": "dismiss"},
    ],
    ids=["answered", "closed"],
)
def test_a_question_alerts_when_asked_and_is_retracted_when_resolved(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, object]
) -> None:
    agent = seed_agent(db_conn)
    question = post_question(client, agent)
    tag = f"question:{question}"
    assert question_notifications(db_conn, agent) == [
        ("alert", tag, "Which surfaces?", f"#{agent}")
    ]

    path = f"/api/keyed/v1/agents/{agent}/questions/{question}/resolve"
    assert client.post(path, json=body, headers=keyed_headers(str(uuid4()))).status_code == 201

    assert question_notifications(db_conn, agent)[1:] == [
        ("retract", tag, "Which surfaces?", f"#{agent}")
    ]


def test_a_rejected_answer_leaves_the_question_notification_standing(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    question = post_question(client, agent)
    path = f"/api/keyed/v1/agents/{agent}/questions/{question}/resolve"

    rejected = client.post(
        path,
        json={"action": "answer", "answer": {"option_ids": ["missing"]}},
        headers=keyed_headers(str(uuid4())),
    )

    assert rejected.status_code == 422
    assert [action for action, *_ in question_notifications(db_conn, agent)] == ["alert"]


def test_each_open_question_has_its_own_notification(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    for title in ("First?", "Second?"):
        asked = client.post(
            f"/api/agents/{agent}/asks",
            json={"title": title, "question": {"type": "text"}},
            headers=keyed_headers(str(uuid4())),
        )
        assert asked.status_code == 201, asked.text
    rows = question_notifications(db_conn, agent)
    assert [(action, subject) for action, _, subject, _ in rows] == [
        ("alert", "First?"),
        ("alert", "Second?"),
    ]
    assert rows[0][1] != rows[1][1]
