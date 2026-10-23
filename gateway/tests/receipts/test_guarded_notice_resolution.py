from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.events.live.bus import EventBus
from gateway.app import openapi_schema
from gateway.tests.auth.gateway_config import GatewayTestClient
from gateway.tests.events.test_notices_endpoint import insert_notice, seed_agent
from gateway.tests.receipts.test_current_notice_receipts import HEADERS
from gateway.tests.receipts.test_current_notice_receipts import client as client
from gateway.tests.support.live_events.faults import unpublishable_bus


def _path(agent: int, notice: int) -> str:
    return f"/api/keyed/v1/agents/{agent}/notices/{notice}/resolve"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": "intent"},
        {"Idempotency-Scope": "principal-v1"},
        {**HEADERS, "Idempotency-Key": ""},
        {**HEADERS, "Idempotency-Key": "x" * 129},
        {**HEADERS, "Idempotency-Scope": "wrong"},
    ],
)
def test_required_identity_rejects_before_effects(
    client: TestClient, db_conn: psycopg.Connection, headers: dict[str, str]
) -> None:
    agent = seed_agent(db_conn)
    notice = insert_notice(db_conn, agent, "Question", require_response=True)
    result = client.post(
        _path(agent, notice), json={"action": "answer", "reply": "yes"}, headers=headers
    )
    assert result.status_code == 422
    assert db_conn.execute(
        "SELECT resolved_at FROM agent_notices WHERE id=%s", (notice,)
    ).fetchone() == (None,)
    assert db_conn.execute("SELECT count(*) FROM notice_operation_receipts").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)


def test_concurrent_answer_reply_replays_one_inbound(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    notice = insert_notice(db_conn, agent, "Question", require_response=True)
    path = _path(agent, notice)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                client.post, path, json={"action": "answer", "reply": "yes"}, headers=HEADERS
            )
            for _ in range(4)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert all(result.status_code == 201 for result in results), [r.text for r in results]
    assert len({r.json()["inbound_id"] for r in results}) == 1
    assert (
        client.post(
            path, json={"action": "answer", "reply": "changed"}, headers=HEADERS
        ).status_code
        == 409
    )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)


def test_lost_tail_replays_after_notice_deletion_without_touching_later_row(
    client: GatewayTestClient, db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    agent = seed_agent(db_conn)
    notice = insert_notice(db_conn, agent, "A", require_response=True)
    path = _path(agent, notice)
    body = {"action": "answer", "reply": "yes"}

    client.app.state.bus = unpublishable_bus()
    assert client.post(path, json=body, headers=HEADERS).status_code == 500
    client.app.state.bus = event_bus
    original = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone()
    assert original is not None
    later = insert_notice(db_conn, agent, "B", require_response=True)
    db_conn.execute("DELETE FROM agent_notices WHERE id=%s", (notice,))
    db_conn.commit()
    replay = client.post(path, json=body, headers=HEADERS)
    assert replay.status_code == 201 and replay.json()["inbound_id"] == original[0]
    assert db_conn.execute(
        "SELECT resolved_at FROM agent_notices WHERE id=%s", (later,)
    ).fetchone() == (None,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)


@pytest.mark.parametrize("remove", [False, True])
def test_consumed_original_reply_does_not_resurrect(
    client: TestClient, db_conn: psycopg.Connection, remove: bool
) -> None:
    agent = seed_agent(db_conn)
    notice = insert_notice(db_conn, agent, "A", require_response=True)
    path = _path(agent, notice)
    body = {"action": "answer", "reply": "yes"}
    first = client.post(path, json=body, headers=HEADERS)
    assert first.status_code == 201
    inbound_id = first.json()["inbound_id"]
    if remove:
        db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (inbound_id,))
    else:
        db_conn.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (inbound_id,))
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s", (agent,)
    )
    db_conn.commit()
    replay = client.post(path, json=body, headers=HEADERS)
    assert replay.status_code == 201 and replay.json()["inbound_id"] == inbound_id
    assert replay.json()["status"] == "terminated"
    assert db_conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent,)).fetchone() == (
        "terminated",
    )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0 if remove else 1,)


def test_authentication_is_rechecked_on_replay(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    agent = seed_agent(db_conn)
    notice = insert_notice(db_conn, agent, "Question", require_response=True)
    path = _path(agent, notice)
    body = {"action": "answer", "reply": "yes"}
    first = client.post(path, json=body, headers=HEADERS)
    assert first.status_code == 201
    assert (
        client.post(
            path, json=body, headers={**HEADERS, "Authorization": "Bearer revoked"}
        ).status_code
        == 401
    )


def test_openapi_requires_both_identity_headers() -> None:
    route = openapi_schema()["paths"][
        "/api/keyed/v1/agents/{agent_id}/notices/{notice_id}/resolve"
    ]["post"]
    headers = {
        param["name"]: param["required"] for param in route["parameters"] if param["in"] == "header"
    }
    assert headers["Idempotency-Key"] and headers["Idempotency-Scope"]
