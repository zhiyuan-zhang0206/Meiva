from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from base.api_contracts.idempotency import keyed_headers
from gateway.tests.events.test_notices_endpoint import seed_agent
from gateway.tests.receipts.test_guarded_notice_resolution import client as client
from gateway.tests.receipts.test_notice_questions import QUESTION, post_question


def test_accepted_read_replays_without_reopening_old_action(
    client: TestClient,
    db_conn: psycopg.Connection,
) -> None:
    agent = seed_agent(db_conn)
    notice = post_question(client, agent, {"type": "text"})
    path = f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve"
    headers = keyed_headers(str(uuid4()))
    first = client.post(path, json={"action": "answer", "reply": "Received"}, headers=headers)
    assert first.status_code == 201
    db_conn.execute(
        "UPDATE notice_operation_receipts SET request=jsonb_set(request, '{action}', '\"read\"') WHERE path=%s",
        (path,),
    )
    db_conn.execute(
        "UPDATE agent_notices SET require_response=false, resolution='read', question=NULL, answer=NULL WHERE id=%s",
        (notice,),
    )
    db_conn.commit()
    body = {"action": "read", "reply": "Received"}
    replay = client.post(path, json=body, headers=headers)
    assert replay.status_code == 201 and replay.json()["inbound_id"] == first.json()["inbound_id"]
    assert client.post(path, json=body, headers=keyed_headers(str(uuid4()))).status_code == 422
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)


def test_legacy_other_is_readable_but_cannot_be_submitted_again(
    client: TestClient,
    db_conn: psycopg.Connection,
) -> None:
    agent = seed_agent(db_conn)
    notice = post_question(client, agent)
    question = QUESTION | {"allow_other": True}
    db_conn.execute("UPDATE agent_notices SET question=%s WHERE id=%s", (Jsonb(question), notice))
    db_conn.commit()
    assert client.get("/api/notices").json()["awaiting"][0]["question"] == question
    rejected = client.post(
        f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve",
        json={"action": "answer", "answer": {"other_text": "CLI"}},
        headers=keyed_headers(str(uuid4())),
    )
    assert rejected.status_code == 422
    answer: dict[str, object] = {
        "option_ids": [],
        "other_text": "CLI",
        "text": None,
        "comment": "Keep the original meaning",
    }
    db_conn.execute(
        "UPDATE agent_notices SET answer=%s, reply='Other: CLI', resolved_at=now(), resolution='answered' WHERE id=%s",
        (Jsonb(answer), notice),
    )
    db_conn.commit()
    history = client.get("/api/notices").json()["resolved_page"]
    assert history[0]["question"] == question and history[0]["answer"] == answer
