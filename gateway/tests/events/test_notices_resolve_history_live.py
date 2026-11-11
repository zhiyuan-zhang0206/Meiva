from collections.abc import Callable
from uuid import uuid4

import psycopg
import pytest

from base.agents.observation.snapshot import select_one
from base.api_contracts.idempotency import keyed_headers
from base.config import settings
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.test_notices_endpoint import insert_notice, pending_rows, seed_agent
from gateway.tests.support.live_events.recorder import recording_live_events


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
        default_model=settings.lm.llm_model,
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
        default_model=settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [nid]


def test_open_default_limit_comes_from_display_config(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    insert_notice(db_conn, a, "older", require_response=True)
    insert_notice(db_conn, a, "newer", require_response=True)
    insert_notice(db_conn, a, "newest", require_response=True)

    with gateway_test_client(fields={"notices_open_default_limit": 2}) as client:
        data = client.get("/api/notices/open").json()
    assert [r["title"] for r in data] == ["older", "newer"]


def test_feed_defaults_come_from_display_config(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    insert_notice(db_conn, a, "open older", require_response=True)
    insert_notice(db_conn, a, "open newer", require_response=True)

    with gateway_test_client(fields={"notices_open_default_limit": 1}) as client:
        feed = client.get("/api/notices").json()
    assert [r["title"] for r in feed["awaiting"]] == ["open older"]


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


# meiva:note
# The published event carries the GLOBAL id, not the agent-local one: the SDK's id
# space is local and the frontend feed matches on global ids, so a local id here
# leaves the resolved row in the open list until a refresh.
# meiva:link gateway/agents/docs/notice-receipts.md
def test_resolution_publishes_global_notice_id(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    agent_id = seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM agent_notices")
        row = cur.fetchone()
        assert row is not None
        local_skew = int(row[0]) + 100
    open_id = insert_notice(db_conn, agent_id, "open notice", require_response=True)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agent_notices SET local_id = %s WHERE id = %s", (local_skew, open_id))
    db_conn.commit()

    with recording_live_events(event_bus) as live, gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{agent_id}/notices/{open_id}/resolve",
            json={"action": "dismiss"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
        published = [event.payload["notice_id"] for event in live.settle("notice_resolved", 1)]
    assert resp.status_code == 201, resp.text
    assert published == [open_id], f"expected global id {open_id}, got {published}"
