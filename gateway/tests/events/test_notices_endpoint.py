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


def insert_notice(
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
            "INSERT INTO agent_notices (agent_id, title, content, resolved_at, resolution, reply) "
            "VALUES (%s, %s, %s, %s::timestamptz, %s, %s) RETURNING id",
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
    n1 = insert_notice(
        db_conn,
        a,
        "deploy to prod?",
        content="A) yes\nB) no",
    )
    n2 = insert_notice(db_conn, a, "name the branch?")
    insert_notice(
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
    assert [n.id for n in snap.notices_awaiting_response] == [n1, n2]
    assert snap.notices_awaiting_response[0].title == "deploy to prod?"
    assert snap.notices_awaiting_response[0].content == "A) yes\nB) no"
    assert snap.notices_awaiting_response[1].content is None


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
    assert snap.notices_awaiting_response == []


def test_snapshot_scoped_by_agent(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    b = seed_agent(db_conn)
    na = insert_notice(db_conn, a, "for a")
    insert_notice(db_conn, b, "for b")

    snap_a = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model=settings.lm.llm_model,
    )
    assert snap_a is not None
    assert [n.id for n in snap_a.notices_awaiting_response] == [na]


def test_open_feed_oldest_first(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents SET label = 'agent-a' WHERE id = %s", (a,))
    db_conn.commit()
    insert_notice(db_conn, a, "older")
    middle = insert_notice(db_conn, a, "middle")
    insert_notice(db_conn, a, "newer")

    with gateway_test_client() as client:
        resp = client.get("/api/notices/open")
    assert resp.status_code == 200
    data = resp.json()
    assert [r["title"] for r in data] == ["older", "middle", "newer"]
    assert data[1]["id"] == middle
    assert data[0]["agent_label"] == "agent-a"
    assert data[0]["resolved_at"] is None


def test_notices_feed_returns_awaiting_questions(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    aw1 = insert_notice(db_conn, a, "decision one")
    aw2 = insert_notice(db_conn, a, "decision two")
    insert_notice(
        db_conn,
        a,
        "done one",
        resolved_at="2026-06-14T02:00:00Z",
        resolution="answered",
        reply="ok",
    )
    insert_notice(
        db_conn,
        a,
        "done two",
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="ok",
    )
    insert_notice(
        db_conn,
        a,
        "done three",
        resolved_at="2026-06-14T00:00:00Z",
        resolution="dismissed",
    )

    with gateway_test_client() as client:
        feed = client.get("/api/notices").json()

    assert list(feed) == ["awaiting"]
    assert [n["title"] for n in feed["awaiting"]] == ["decision one", "decision two"]
    assert [n["id"] for n in feed["awaiting"]] == [aw1, aw2]


def test_notices_feed_excludes_resolved_from_open_lists(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    insert_notice(
        db_conn,
        a,
        "was decision",
        resolved_at="2026-06-14T00:00:00Z",
        resolution="answered",
        reply="ok",
    )
    with gateway_test_client() as client:
        feed = client.get("/api/notices").json()
    assert feed["awaiting"] == []


def test_answer_marks_and_delivers_inbound(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "send the records-release email?")
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "yes, send it"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 201
    assert "status" in resp.json()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT resolved_at, resolution, reply FROM agent_notices WHERE id = %s", (nid,)
        )
        row = cur.fetchone()
    assert row is not None
    resolved_at, resolution, reply = row
    assert resolved_at is not None
    assert resolution == "answered"
    assert reply == "yes, send it"

    rows = pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:notice-reply"
    assert "send the records-release email?" in inbound_text
    assert "yes, send it" in inbound_text

    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model=settings.lm.llm_model,
    )
    assert snap is not None
    assert snap.notices_awaiting_response == []


def test_answer_without_reply_422(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "q?")
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer"},
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
    assert [n.id for n in snap.notices_awaiting_response] == [nid]
    assert pending_rows(db_conn, a) == []


def test_answer_empty_reply_422(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "q?")
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "   "},
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
    assert [n.id for n in snap.notices_awaiting_response] == [nid]
    assert pending_rows(db_conn, a) == []


def test_dismiss_delivers_system_note(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "name the branch?")
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "dismiss"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 201

    with db_conn.cursor() as cur:
        cur.execute("SELECT resolution FROM agent_notices WHERE id = %s", (nid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "dismissed"

    rows = pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:notice-dismiss"
    assert "name the branch?" in inbound_text
    assert "dismissed" in inbound_text.lower()


def test_answer_publishes_notice_resolved(db_conn: psycopg.Connection, event_bus: EventBus) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "ship it?")
    with published_events(event_bus) as payloads, gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "yes"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 201
    resolved = [e for e in payloads if e.get("role") == "notice_resolved"]
    assert len(resolved) == 1
    event = resolved[0]
    assert event["notice_id"] == nid
