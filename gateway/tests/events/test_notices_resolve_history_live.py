from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from base.agents.observation.snapshot import select_one
from base.api_contracts.idempotency import keyed_headers
from base.config import settings
from base.lm.catalog import ModelCatalog
from gateway.tests.events.test_notices_endpoint import insert_notice, pending_rows, seed_agent
from tests.fixtures.gateway_config import gateway_test_client


@pytest.mark.parametrize("reply", [None, "Acknowledged"])
@pytest.mark.parametrize("require_response", [False, True])
def test_new_read_operations_are_rejected_without_effects(
    db_conn: psycopg.Connection,
    reply: str | None,
    require_response: bool,
) -> None:
    agent = seed_agent(db_conn)
    notice = insert_notice(db_conn, agent, "Question", require_response=require_response)
    with gateway_test_client() as client:
        result = client.post(
            f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve",
            json={"action": "read", "reply": reply},
            headers=keyed_headers(str(uuid4())),
        )
    assert result.status_code == 422
    assert db_conn.execute(
        "SELECT resolved_at, reply FROM agent_notices WHERE id=%s", (notice,)
    ).fetchone() == (None, None)
    assert pending_rows(db_conn, agent) == []
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (0,)


def test_read_keeps_question_in_snapshot(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "needs answer", require_response=True)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "read"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 422
    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [nid]
    assert pending_rows(db_conn, a) == []


def test_resolve_nonexistent_409(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/999999/resolve",
            json={"action": "dismiss"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 409
    assert pending_rows(db_conn, a) == []


def test_resolve_twice_second_is_409_and_delivers_once(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "q?", require_response=True)
    with gateway_test_client() as client:
        first = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "one"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
        second = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "two"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert first.status_code == 201
    assert second.status_code == 409
    assert len(pending_rows(db_conn, a)) == 1


def test_resolve_cross_agent_path_409(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    b = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "for a", require_response=True)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{b}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "x"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 409
    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [nid]


def test_open_default_limit_comes_from_display_config(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    from base.config import settings

    monkeypatch.setattr(settings.display, "notices_open_default_limit", 2)
    a = seed_agent(db_conn)
    insert_notice(db_conn, a, "older", require_response=True)
    insert_notice(db_conn, a, "newer", require_response=True)
    insert_notice(db_conn, a, "newest", require_response=True)

    with gateway_test_client() as client:
        data = client.get("/api/notices/open").json()
    assert [r["title"] for r in data] == ["older", "newer"]


def test_feed_defaults_come_from_display_config(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    from base.config import settings

    monkeypatch.setattr(settings.display, "notices_open_default_limit", 1)
    monkeypatch.setattr(settings.display, "notices_resolved_default_page", 1)
    a = seed_agent(db_conn)
    insert_notice(db_conn, a, "open older", require_response=True)
    insert_notice(db_conn, a, "open newer", require_response=True)
    insert_notice(
        db_conn,
        a,
        "resolved r0",
        require_response=True,
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="x",
    )
    insert_notice(
        db_conn,
        a,
        "resolved r1",
        require_response=True,
        resolved_at="2026-06-14T02:00:00Z",
        resolution="answered",
        reply="x",
    )

    with gateway_test_client() as client:
        feed = client.get("/api/notices").json()
    assert [r["title"] for r in feed["awaiting"]] == ["open older"]
    assert [r["title"] for r in feed["resolved_page"]] == ["resolved r1"]


def test_check_constraints_reject_illegal_states(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    ts = "2026-06-15T01:00:00Z"

    def _raw_insert(
        *,
        require_response: bool,
        blocking: bool = False,
        resolved_at: str | None = None,
        resolution: str | None = None,
        reply: str | None = None,
    ) -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_notices "
                "(agent_id, local_id, title, priority, require_response, blocking, "
                "resolved_at, resolution, reply, expire_at) "
                "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, 'x', 'P2', %s, %s, %s::timestamptz, %s, %s, now() + interval '1 day')",
                (a, a, require_response, blocking, resolved_at, resolution, reply),
            )

    def _expect_violation(insert: Callable[[], None]) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            insert()
        db_conn.rollback()

    _expect_violation(lambda: _raw_insert(require_response=False, blocking=True))
    _expect_violation(lambda: _raw_insert(require_response=True, resolved_at=ts))
    _expect_violation(lambda: _raw_insert(require_response=True, resolution="answered", reply="x"))
    _expect_violation(lambda: _raw_insert(require_response=True, resolved_at=ts, resolution="read"))
    _expect_violation(
        lambda: _raw_insert(require_response=True, resolved_at=ts, resolution="answered")
    )

    _raw_insert(require_response=True, resolved_at=ts, resolution="superseded")
    _raw_insert(require_response=False, resolved_at=ts, resolution="superseded")
    _raw_insert(require_response=True, resolved_at=ts, resolution="expired")
    _raw_insert(require_response=False, resolved_at=ts, resolution="expired")

    _raw_insert(require_response=True, resolved_at=ts, resolution="answered", reply="ok")
    _raw_insert(require_response=False, resolved_at=ts, resolution="read")
    _raw_insert(require_response=False, resolved_at=ts, resolution="answered", reply="ok")
    db_conn.commit()


def test_live_returns_new_open_notices_oldest_first(
    db_conn: psycopg.Connection,
) -> None:
    a1 = seed_agent(db_conn)
    a2 = seed_agent(db_conn)
    insert_notice(db_conn, a1, "FYI one", require_response=False)
    n2 = insert_notice(db_conn, a2, "Decision", require_response=True)
    insert_notice(db_conn, a1, "FYI two", require_response=False)

    with gateway_test_client() as client:
        r = client.get("/api/notices/live", params={"after": 0})
        assert r.status_code == 200
        items = r.json()
        assert [it["id"] for it in items] == [n2]
        assert items[0]["title"] == "Decision"
        assert items[0]["require_response"] is True
        assert items[0]["agent_id"] == a2

        r2 = client.get("/api/notices/live", params={"after": n2})
        assert r2.status_code == 200
        assert [it["id"] for it in r2.json()] == []


def test_live_excludes_resolved(db_conn: psycopg.Connection) -> None:
    a1 = seed_agent(db_conn)
    n1 = insert_notice(db_conn, a1, "Open question", require_response=True)
    insert_notice(
        db_conn,
        a1,
        "Stay",
        require_response=False,
        resolved_at="2026-08-06T00:00:00+00:00",
        resolution="read",
    )

    with gateway_test_client() as client:
        r = client.get("/api/notices/live", params={"after": 0})
        assert r.status_code == 200
        assert [it["id"] for it in r.json()] == [n1]


def test_old_include_awaiting_flag_cannot_restore_fyi(
    db_conn: psycopg.Connection,
) -> None:
    a1 = seed_agent(db_conn)
    insert_notice(db_conn, a1, "FYI one", require_response=False)
    insert_notice(db_conn, a1, "Decision", require_response=True)

    with gateway_test_client() as client:
        default = client.get("/api/notices/open").json()
        assert [it["title"] for it in default] == ["Decision"]
        both = client.get("/api/notices/open", params={"include_awaiting": True}).json()
        assert {it["title"] for it in both} == {"Decision"}
        insert_notice(
            db_conn,
            a1,
            "Gone",
            require_response=False,
            resolved_at="2026-08-06T00:00:00+00:00",
            resolution="read",
        )
        both2 = client.get("/api/notices/open", params={"include_awaiting": True}).json()
        assert all(it["title"] != "Gone" for it in both2)


def test_live_empty_after_cursor(db_conn: psycopg.Connection) -> None:
    a1 = seed_agent(db_conn)
    n1 = insert_notice(db_conn, a1, "Only", require_response=False)

    with gateway_test_client() as client:
        r = client.get("/api/notices/live", params={"after": n1})
        assert r.status_code == 200
        assert r.json() == []


def test_supersede_publishes_global_notice_id(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ops import lifecycle as ops_lifecycle

    agent_id = seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM agent_notices")
        row = cur.fetchone()
        assert row is not None
        local_skew = int(row[0]) + 100
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, reply, resolved_at, resolution, expire_at) "
            "VALUES (%s, %s, 'old closed', 'P2', TRUE, 'old reply', now(), 'answered', now() + interval '1 day')",
            (agent_id, local_skew),
        )
    db_conn.commit()
    open_id = insert_notice(db_conn, agent_id, "open notice")
    with db_conn.cursor() as cur:
        cur.execute("SELECT local_id FROM agent_notices WHERE id = %s", (open_id,))
        row = cur.fetchone()
        assert row is not None
        open_local = int(row[0])
    assert open_local != open_id, "test setup: local id must differ from the global id"

    published: list[int] = []

    async def _capture(_bus: object, _agent_id: int, notice_id: int, **_: object) -> None:
        published.append(notice_id)

    monkeypatch.setattr(ops_lifecycle, "publish_notice_resolved", _capture)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{agent_id}/notices",
            json={"title": "new notice", "content": None},
            headers=keyed_headers(str(uuid4())),
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["superseded"] == [open_local]
    assert published == [open_id], f"expected global id {open_id}, got {published}"


def test_post_notice_sets_default_expire_at(db_conn: psycopg.Connection) -> None:
    aid = seed_agent(db_conn)
    before = datetime.now(UTC)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{aid}/notices",
            json={"title": "default expiry notice", "content": "detail"},
            headers=keyed_headers(str(uuid4())),
        )
    assert resp.status_code == 201
    after = datetime.now(UTC)

    with db_conn.cursor() as cur:
        cur.execute("SELECT expire_at FROM agent_notices WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
    assert row is not None
    expire_at = row[0]
    expected_sec = settings.daemon.notice_ttl_limit_seconds
    assert (
        before + timedelta(seconds=expected_sec - 5)
        <= expire_at
        <= after + timedelta(seconds=expected_sec + 5)
    )


def test_post_notice_with_explicit_expire_at(db_conn: psycopg.Connection) -> None:
    aid = seed_agent(db_conn)
    target = datetime.now(UTC) + timedelta(hours=2)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{aid}/notices",
            json={
                "title": "explicit expiry notice",
                "expire_at": target.isoformat(),
            },
            headers=keyed_headers(str(uuid4())),
        )
    assert resp.status_code == 201

    with db_conn.cursor() as cur:
        cur.execute("SELECT expire_at FROM agent_notices WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
    assert row is not None
    expire_at = row[0]
    assert abs((expire_at - target).total_seconds()) < 2.0


def test_post_notice_clamps_expire_at_when_exceeding_limit(db_conn: psycopg.Connection) -> None:
    aid = seed_agent(db_conn)
    now = datetime.now(UTC)
    target = now + timedelta(days=10)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{aid}/notices",
            json={
                "title": "exceeding expiry notice",
                "expire_at": target.isoformat(),
            },
            headers=keyed_headers(str(uuid4())),
        )
    assert resp.status_code == 201

    with db_conn.cursor() as cur:
        cur.execute("SELECT expire_at FROM agent_notices WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
    assert row is not None
    expire_at = row[0]
    expected_max = now + timedelta(seconds=settings.daemon.notice_ttl_limit_seconds)
    assert abs((expire_at - expected_max).total_seconds()) < 5.0


def test_post_notice_past_expire_at_is_422(db_conn: psycopg.Connection) -> None:
    aid = seed_agent(db_conn)
    past = datetime.now(UTC) - timedelta(minutes=10)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{aid}/notices",
            json={
                "title": "past expiry notice",
                "expire_at": past.isoformat(),
            },
            headers=keyed_headers(str(uuid4())),
        )
    assert resp.status_code == 422
    assert "expire_at is in the past" in resp.json()["detail"]
