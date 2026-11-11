from collections.abc import Mapping, Sequence
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx import Response
from psycopg.types.json import Jsonb

from base.api_contracts.idempotency import keyed_headers
from gateway.tests.events.test_questions_endpoint import answers_path, seed_agent
from gateway.tests.receipts.test_guarded_question_resolution import client as client

QUESTION: dict[str, object] = {
    "type": "multi_select",
    "options": [
        {"id": "web", "label": "Web", "description": None},
        {"id": "desktop", "label": "Desktop", "description": None},
    ],
    "allow_other": False,
}
TEXT: dict[str, object] = {"type": "text"}


def ask(client: TestClient, agent: int, *items: tuple[str, dict[str, object]]) -> list[int]:
    response = client.post(
        f"/api/agents/{agent}/asks",
        json={
            "questions": [
                {"title": title, "content": None, "question": question} for title, question in items
            ]
        },
        headers=keyed_headers(str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.json()["ids"]


def submit(
    client: TestClient,
    agent: int,
    expected: list[int],
    results: Sequence[Mapping[str, object]],
    key: str | None = None,
) -> Response:
    return client.post(
        answers_path(agent),
        json={"expected_ids": expected, "results": results},
        headers=keyed_headers(key or str(uuid4())),
    )


def inbound(conn: psycopg.Connection, agent: int) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT source, content FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent,)
        ).fetchall()
    ]


def test_one_ask_returns_each_question_its_own_id_in_order_and_replays(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    body: dict[str, list[dict[str, object]]] = {
        "questions": [
            {"title": "First?", "question": TEXT},
            {"title": "Second?", "question": QUESTION},
            {"title": "Third?", "question": TEXT},
        ]
    }
    headers = keyed_headers("one-call")
    first = client.post(f"/api/agents/{agent}/asks", json=body, headers=headers)
    assert first.status_code == 201, first.text
    ids = first.json()["ids"]
    assert len(ids) == 3 and ids == sorted(ids)
    assert client.post(f"/api/agents/{agent}/asks", json=body, headers=headers).json() == {
        "ids": ids
    }
    changed = {"questions": body["questions"][:2]}
    assert (
        client.post(f"/api/agents/{agent}/asks", json=changed, headers=headers).status_code == 409
    )
    feed = client.get(f"/api/questions/open?agent_id={agent}").json()
    assert [row["id"] for row in feed] == ids
    assert [row["title"] for row in feed] == ["First?", "Second?", "Third?"]


def test_an_empty_or_invalid_ask_inserts_nothing(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    for body in (
        {"questions": []},
        {"questions": [{"title": "ok", "question": TEXT}, {"title": " ", "question": TEXT}]},
        {"questions": [{"title": "legacy", "question": QUESTION | {"allow_other": True}}]},
    ):
        response = client.post(
            f"/api/agents/{agent}/asks", json=body, headers=keyed_headers(str(uuid4()))
        )
        assert response.status_code == 422, response.text
    assert db_conn.execute(
        "SELECT count(*) FROM agent_questions WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


def test_one_submission_settles_every_open_question_with_one_inbound_in_id_order(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    first, second, third = ask(
        client, agent, ("Which surfaces?", QUESTION), ("Ship v2 now?", TEXT), ("Scope?", TEXT)
    )
    done = submit(
        client,
        agent,
        [third, first, second],
        [
            {"id": first, "answer": {"option_ids": ["desktop"], "comment": "Use 1 later."}},
            {"id": second, "skip": True},
            {"id": third, "reply": "keep it small"},
        ],
    )
    assert done.status_code == 201, done.text
    rows = db_conn.execute(
        "SELECT id, resolution, reply FROM agent_questions WHERE agent_id=%s ORDER BY id", (agent,)
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        (first, "answered"),
        (second, "dismissed"),
        (third, "answered"),
    ]
    assert rows[1][2] is None
    [(source, content)] = inbound(db_conn, agent)
    assert source == "system:question-reply"
    blocks = content.split("\n\nRe: question #")
    assert [block.split("\n", 1)[0] for block in blocks] == [
        f'Re: question #{first} "Which surfaces?"',
        f'{second} "Ship v2 now?"',
        f'{third} "Scope?"',
    ]
    assert "Comment: Use 1 later." in blocks[0]
    assert blocks[1].endswith("Skipped by the user.")
    assert "keep it small" in blocks[2]
    assert client.get("/api/questions").json()["awaiting"] == []


def test_skipping_everything_is_one_dismissal_inbound(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    ids = ask(client, agent, ("A?", TEXT), ("B?", TEXT))
    done = submit(client, agent, ids, [{"id": i, "skip": True} for i in ids])
    assert done.status_code == 201, done.text
    assert (
        db_conn.execute(
            "SELECT resolution, reply, answer FROM agent_questions WHERE agent_id=%s ORDER BY id",
            (agent,),
        ).fetchall()
        == [("dismissed", None, None)] * 2
    )
    [(source, content)] = inbound(db_conn, agent)
    assert source == "system:question-dismiss"
    assert content.count("Skipped by the user.") == 2


@pytest.mark.parametrize(
    "answer", [{}, {"option_ids": ["missing"]}, {"text": "yes"}, {"other_text": " "}]
)
def test_one_invalid_answer_settles_nothing(
    client: TestClient, db_conn: psycopg.Connection, answer: dict[str, object]
) -> None:
    agent = seed_agent(db_conn)
    first, second = ask(client, agent, ("Choose?", QUESTION), ("Why?", TEXT))
    response = submit(
        client,
        agent,
        [first, second],
        [{"id": first, "answer": answer}, {"id": second, "reply": "because"}],
    )
    assert response.status_code == 422, response.text
    assert db_conn.execute(
        "SELECT count(*) FROM agent_questions WHERE agent_id=%s AND resolved_at IS NOT NULL",
        (agent,),
    ).fetchone() == (0,)
    assert inbound(db_conn, agent) == []


def test_results_that_do_not_match_the_expected_ids_are_refused(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    first, second = ask(client, agent, ("A?", TEXT), ("B?", TEXT))
    for expected, results in (
        ([first, second], [{"id": first, "reply": "x"}]),
        ([first], [{"id": first, "reply": "x"}, {"id": second, "reply": "y"}]),
        ([first, second], [{"id": first, "reply": "x"}, {"id": first, "reply": "y"}]),
        ([first, first], [{"id": first, "reply": "x"}, {"id": first, "reply": "y"}]),
        ([first], [{"id": first, "reply": "x", "skip": True}]),
        ([first], [{"id": first}]),
    ):
        assert submit(client, agent, expected, results).status_code == 422
    assert inbound(db_conn, agent) == []


def test_a_changed_open_set_settles_nothing_and_returns_the_latest_set(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    first, second = ask(client, agent, ("A?", TEXT), ("B?", TEXT))
    [third] = ask(client, agent, ("C?", TEXT))
    stale = submit(
        client,
        agent,
        [first, second],
        [{"id": first, "reply": "x"}, {"id": second, "skip": True}],
    )
    assert stale.status_code == 409
    assert [row["id"] for row in stale.json()["open"]] == [first, second, third]
    assert inbound(db_conn, agent) == []

    closed = client.post(
        f"/api/agents/{agent}/asks/{second}/close", headers=keyed_headers(str(uuid4()))
    )
    assert closed.status_code == 200
    again = submit(
        client,
        agent,
        [first, second, third],
        [{"id": first, "reply": "x"}, {"id": second, "skip": True}, {"id": third, "skip": True}],
    )
    assert again.status_code == 409
    assert [row["id"] for row in again.json()["open"]] == [first, third]

    ok = submit(
        client, agent, [first, third], [{"id": first, "reply": "x"}, {"id": third, "skip": True}]
    )
    assert ok.status_code == 201, ok.text
    assert len(inbound(db_conn, agent)) == 1


def test_another_agents_questions_are_not_part_of_the_submission(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    mine, other = seed_agent(db_conn), seed_agent(db_conn)
    [my_question] = ask(client, mine, ("Mine?", TEXT))
    [theirs] = ask(client, other, ("Theirs?", TEXT))
    done = submit(client, mine, [my_question], [{"id": my_question, "reply": "yes"}])
    assert done.status_code == 201
    assert [row["id"] for row in client.get("/api/questions/open").json()] == [theirs]
    assert submit(client, mine, [theirs], [{"id": theirs, "skip": True}]).status_code == 409


def test_answering_with_no_open_questions_is_refused(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    assert submit(client, agent, [1], [{"id": 1, "skip": True}]).status_code == 409


def test_legacy_other_is_readable_but_cannot_be_submitted_again(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    [question_id] = ask(client, agent, ("Which?", QUESTION))
    payload = QUESTION | {"allow_other": True}
    db_conn.execute(
        "UPDATE agent_questions SET question=%s WHERE id=%s", (Jsonb(payload), question_id)
    )
    db_conn.commit()
    assert client.get("/api/questions").json()["awaiting"][0]["question"] == payload
    rejected = submit(
        client, agent, [question_id], [{"id": question_id, "answer": {"other_text": "CLI"}}]
    )
    assert rejected.status_code == 422


def pushes(conn: psycopg.Connection, agent: int) -> list[tuple[str, str, str, str | None]]:
    return [
        (row[0], row[1], row[2], row[3])
        for row in conn.execute(
            "SELECT action, tag, subject, body FROM notification_events "
            "WHERE agent_id=%s AND category='core.question' ORDER BY id",
            (agent,),
        ).fetchall()
    ]


def test_an_agents_questions_are_one_push_alerted_once_and_retracted_when_none_remain(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    tag = f"question:agent:{agent}"
    first = ask(client, agent, ("First?", TEXT), ("Second?", TEXT), ("Third?", TEXT))
    assert pushes(db_conn, agent) == [("alert", tag, "3 questions awaiting an answer", f"#{agent}")]
    [fourth] = ask(client, agent, ("Fourth?", TEXT))
    assert pushes(db_conn, agent)[1:] == [
        ("replace", tag, "4 questions awaiting an answer", f"#{agent}")
    ]
    closed = client.post(
        f"/api/agents/{agent}/asks/{fourth}/close", headers=keyed_headers(str(uuid4()))
    )
    assert closed.status_code == 200
    assert pushes(db_conn, agent)[2:] == [
        ("replace", tag, "3 questions awaiting an answer", f"#{agent}")
    ]
    done = submit(client, agent, first, [{"id": i, "skip": True} for i in first])
    assert done.status_code == 201, done.text
    assert [action for action, *_ in pushes(db_conn, agent)] == [
        "alert",
        "replace",
        "replace",
        "retract",
    ]


def test_a_single_question_alerts_with_its_own_title_and_a_rejected_answer_keeps_the_push(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    [question_id] = ask(client, agent, ("Deploy now?", QUESTION))
    assert pushes(db_conn, agent) == [
        ("alert", f"question:agent:{agent}", "Deploy now?", f"#{agent}")
    ]
    rejected = submit(
        client, agent, [question_id], [{"id": question_id, "answer": {"option_ids": ["missing"]}}]
    )
    assert rejected.status_code == 422
    assert [action for action, *_ in pushes(db_conn, agent)] == ["alert"]
