from uuid import uuid4

import psycopg

from base.agents.observation.snapshot import select_one
from base.config import settings
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.sse_support import published_events


def seed_agent(db_conn: psycopg.Connection, status: str = "idling") -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', %s)",
            (new_id, status),
        )
    db_conn.commit()
    return new_id


def answers_path(agent_id: int) -> str:
    return f"/api/keyed/v1/agents/{agent_id}/questions/answers"


def answer_body(question_id: int, reply: str) -> dict[str, object]:
    return {
        "expected_ids": [question_id],
        "results": [{"id": question_id, "reply": reply}],
    }


def skip_body(question_id: int) -> dict[str, object]:
    return {
        "expected_ids": [question_id],
        "results": [{"id": question_id, "skip": True}],
    }


def insert_question(
    db_conn: psycopg.Connection,
    agent_id: int,
    title: str,
    *,
    content: str | None = None,
    resolved_at: str | None = None,
    resolution: str | None = None,
    reply: str | None = None,
) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_questions (agent_id, title, content, question, resolved_at, resolution, reply) "
            'VALUES (%s, %s, %s, \'{"type": "text"}\', %s::timestamptz, %s, %s) RETURNING id',
            (agent_id, title, content, resolved_at, resolution, reply),
        )
        row = cur.fetchone()
        assert row is not None
        nid = row[0]
    db_conn.commit()
    return nid


def pending_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status, content, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def test_snapshot_lists_awaiting_oldest_first(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    n1 = insert_question(
        db_conn,
        a,
        "deploy to prod?",
        content="A) yes\nB) no",
    )
    n2 = insert_question(db_conn, a, "name the branch?")
    insert_question(
        db_conn,
        a,
        "already decided",
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="done",
    )

    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model=settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.id for n in snap.questions_awaiting_response] == [n1, n2]
    assert snap.questions_awaiting_response[0].title == "deploy to prod?"
    assert snap.questions_awaiting_response[0].content == "A) yes\nB) no"
    assert snap.questions_awaiting_response[1].content is None


def test_snapshot_awaiting_empty_when_none(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model=settings.lm.llm_model,
    )
    assert snap is not None
    assert snap.questions_awaiting_response == []


def test_snapshot_scoped_by_agent(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    b = seed_agent(db_conn)
    na = insert_question(db_conn, a, "for a")
    insert_question(db_conn, b, "for b")

    snap_a = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model=settings.lm.llm_model,
    )
    assert snap_a is not None
    assert [n.id for n in snap_a.questions_awaiting_response] == [na]


def test_open_feed_oldest_first(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents SET label = 'agent-a' WHERE id = %s", (a,))
    db_conn.commit()
    insert_question(db_conn, a, "older")
    middle = insert_question(db_conn, a, "middle")
    insert_question(db_conn, a, "newer")

    with gateway_test_client() as client:
        resp = client.get("/api/questions/open")
    assert resp.status_code == 200
    data = resp.json()
    assert [r["title"] for r in data] == ["older", "middle", "newer"]
    assert data[1]["id"] == middle
    assert data[0]["agent_label"] == "agent-a"
    assert data[0]["resolved_at"] is None


def test_questions_feed_returns_awaiting_questions(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    aw1 = insert_question(db_conn, a, "decision one")
    aw2 = insert_question(db_conn, a, "decision two")
    insert_question(
        db_conn,
        a,
        "done one",
        resolved_at="2026-06-14T02:00:00Z",
        resolution="answered",
        reply="ok",
    )
    insert_question(
        db_conn,
        a,
        "done two",
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="ok",
    )
    insert_question(
        db_conn,
        a,
        "done three",
        resolved_at="2026-06-14T00:00:00Z",
        resolution="dismissed",
    )

    with gateway_test_client() as client:
        feed = client.get("/api/questions").json()

    assert list(feed) == ["awaiting"]
    assert [n["title"] for n in feed["awaiting"]] == ["decision one", "decision two"]
    assert [n["id"] for n in feed["awaiting"]] == [aw1, aw2]


def test_questions_feed_excludes_resolved_from_open_lists(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    insert_question(
        db_conn,
        a,
        "was decision",
        resolved_at="2026-06-14T00:00:00Z",
        resolution="answered",
        reply="ok",
    )
    with gateway_test_client() as client:
        feed = client.get("/api/questions").json()
    assert feed["awaiting"] == []


def test_answer_marks_and_delivers_inbound(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    nid = insert_question(db_conn, a, "send the records-release email?")
    with gateway_test_client() as client:
        resp = client.post(
            answers_path(a),
            json=answer_body(nid, "yes, send it"),
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 201
    assert "status" in resp.json()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT resolved_at, resolution, reply FROM agent_questions WHERE id = %s", (nid,)
        )
        row = cur.fetchone()
    assert row is not None
    resolved_at, resolution, reply = row
    assert resolved_at is not None
    assert resolution == "answered"
    assert reply == "Answer: yes, send it"

    rows = pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:question-reply"
    assert "send the records-release email?" in inbound_text
    assert "yes, send it" in inbound_text

    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model=settings.lm.llm_model,
    )
    assert snap is not None
    assert snap.questions_awaiting_response == []


def test_answer_without_reply_422(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    nid = insert_question(db_conn, a, "q?")
    with gateway_test_client() as client:
        resp = client.post(
            answers_path(a),
            json={"expected_ids": [nid], "results": [{"id": nid}]},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 422
    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model=settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.id for n in snap.questions_awaiting_response] == [nid]
    assert pending_rows(db_conn, a) == []


def test_answer_empty_reply_422(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    nid = insert_question(db_conn, a, "q?")
    with gateway_test_client() as client:
        resp = client.post(
            answers_path(a),
            json=answer_body(nid, ""),
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 422
    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model=settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.id for n in snap.questions_awaiting_response] == [nid]
    assert pending_rows(db_conn, a) == []


def test_dismiss_delivers_system_note(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    nid = insert_question(db_conn, a, "name the branch?")
    with gateway_test_client() as client:
        resp = client.post(
            answers_path(a),
            json=skip_body(nid),
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 201

    with db_conn.cursor() as cur:
        cur.execute("SELECT resolution FROM agent_questions WHERE id = %s", (nid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "dismissed"

    rows = pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:question-dismiss"
    assert "name the branch?" in inbound_text
    assert "Skipped by the user." in inbound_text


def test_answer_publishes_question_resolved(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    a = seed_agent(db_conn)
    nid = insert_question(db_conn, a, "ship it?")
    with published_events(event_bus) as payloads, gateway_test_client() as client:
        resp = client.post(
            answers_path(a),
            json=answer_body(nid, "yes"),
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 201
    resolved = [e for e in payloads if e.get("role") == "question_resolved"]
    assert len(resolved) == 1
    event = resolved[0]
    assert event["question_id"] == nid


def test_attention_counts_deduplicate_publications_and_follow_seen_and_answers(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    b = seed_agent(db_conn, "terminated")
    with gateway_test_client() as client:
        assert client.get("/api/attention/counts").json() == {
            "unread_agents": 0,
            "open_questions": 0,
        }
        for agent_id, key, content in (
            (a, "a1", "first"),
            (a, "a2", "second"),
            (b, "b1", "third"),
            (a, "hidden", None),
        ):
            db_conn.execute(
                "INSERT INTO agent_publications (agent_id,source_key,mode,content,suggestions) "
                "VALUES (%s,%s,'sdk',%s,'[]')",
                (agent_id, key, content),
            )
        db_conn.commit()
        question_id = insert_question(db_conn, a, "answer me")
        assert client.get("/api/attention/counts").json() == {
            "unread_agents": 2,
            "open_questions": 1,
        }
        db_conn.execute(
            "INSERT INTO agent_publication_seen (agent_id,seen_id) "
            "SELECT %s,MAX(id) FROM agent_publications WHERE agent_id=%s",
            (a, a),
        )
        db_conn.execute(
            "UPDATE agent_questions SET resolved_at=now(), resolution='dismissed' WHERE id=%s",
            (question_id,),
        )
        db_conn.commit()
        assert client.get("/api/attention/counts").json() == {
            "unread_agents": 1,
            "open_questions": 0,
        }


def test_attention_counts_are_not_capped_by_the_question_feed_limit(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agent_questions (agent_id,title,question) "
        "SELECT %s,'pending', '{\"type\":\"text\"}'::jsonb FROM generate_series(1,501)",
        (a,),
    )
    db_conn.commit()
    with gateway_test_client() as client:
        assert len(client.get("/api/questions/open?limit=1").json()) == 1
        assert client.get("/api/attention/counts").json() == {
            "unread_agents": 0,
            "open_questions": 501,
        }


def test_attention_counts_each_question_in_one_ask_batch(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    with gateway_test_client() as client:
        response = client.post(
            f"/api/agents/{a}/asks",
            json={
                "questions": [
                    {"title": "first decision", "question": {"type": "text"}},
                    {"title": "second decision", "question": {"type": "text"}},
                ]
            },
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
        assert response.status_code == 201
        assert client.get("/api/attention/counts").json() == {
            "unread_agents": 0,
            "open_questions": 2,
        }
