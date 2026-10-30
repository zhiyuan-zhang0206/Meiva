"""Keyed chat delivery and its reconcile endpoint (R3 door ① server side).

The endpoint POST /api/agents/{id}/messages declares
Idempotency.AT_LEAST_ONCE_WITH_KEY: the caller gives one logical message a
stable key. The inbound INSERT owns that key in its transaction, and a same-key
retry resolves the durable row instead of inserting again — even when the
gateway died after COMMIT and before returning its response.

Tests run against the real app + test DB (TestClient + lifespan).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import ExitStack

import psycopg
import pytest

from base.api_contracts.idempotency import keyed_headers
from base.events.live.bus import EventBus
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.support.live_events.faults import unpublishable_bus
from gateway.tests.support.live_events.recorder import recording_live_events
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    completed,
    failed,
    runner_ops,
)

_HOME_RUNNER = "idem-runner"
_VISION_MODEL = "claude-sonnet-5-5"
_TEXT_ONLY_MODEL = "glm-5.2"


@pytest.fixture
def client(gateway_data_plane: None) -> Iterator[GatewayTestClient]:
    del gateway_data_plane
    with gateway_test_client() as c:
        yield c


@pytest.fixture
def agent_id(db_conn: psycopg.Connection) -> int:
    """A minimal agents row (agents + agents_meta) for message delivery, homed on a runner."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES ('idem-test') RETURNING id")
        row = cur.fetchone()
        assert row is not None
        aid = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'running', %s)",
            (aid, _HOME_RUNNER),
        )
    db_conn.commit()
    return int(aid)


def count_inbounds(conn: psycopg.Connection, agent_id: int, content: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND content = %s",
            (agent_id, content),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _set_model(conn: psycopg.Connection, agent_id: int, model: str) -> None:
    conn.execute(
        "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
        (json.dumps({"llm_model": model}), agent_id),
    )
    conn.commit()


def test_reconcile_endpoint_finds_the_durable_inbound(
    client: GatewayTestClient, agent_id: int
) -> None:
    """A browser whose POST timed out can resolve the unknown outcome by key."""
    sent = client.post(
        f"/api/agents/{agent_id}/messages",
        json={"content": "reconcile me", "source": "user"},
        headers=keyed_headers("key-reconcile"),
    )
    assert sent.status_code == 201, sent.text

    receipt = client.post(
        f"/api/agents/{agent_id}/messages/reconcile",
        json={"content": "reconcile me", "source": "user"},
        headers=keyed_headers("key-reconcile"),
    )

    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["inbound_id"] == sent.json()["inbound_id"]


# meiva:note
# A receipt survives model/upload changes after the original commit: the agent moves to a
# model that cannot see images and the upload is gone, yet the same key still resolves.
def test_reconcile_does_not_repeat_mutable_multimodal_validation(
    client: GatewayTestClient, db_conn: psycopg.Connection, agent_id: int
) -> None:
    from base.agents.upload_delivery.paths import agent_upload_dir

    upload_dir = agent_upload_dir(
        agent_id, root=client.app.state.gateway_config.upload_staging_root
    )
    image = upload_dir / "gone.png"
    image.write_bytes(b"test image")
    _set_model(db_conn, agent_id, _VISION_MODEL)

    body = {
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": f"/api/agents/{agent_id}/uploads/gone.png"},
            }
        ],
        "source": "user",
    }
    sent = client.post(
        f"/api/agents/{agent_id}/messages",
        json=body,
        headers=keyed_headers("key-multimodal-reconcile"),
    )
    assert sent.status_code == 201, sent.text

    image.unlink()
    _set_model(db_conn, agent_id, _TEXT_ONLY_MODEL)
    fresh = client.post(
        f"/api/agents/{agent_id}/messages",
        json=body,
        headers=keyed_headers("key-new-intent"),
    )
    assert fresh.status_code == 422, "the current model and uploads gate a new message"

    retried = client.post(
        f"/api/agents/{agent_id}/messages",
        json=body,
        headers=keyed_headers("key-multimodal-reconcile"),
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["inbound_id"] == sent.json()["inbound_id"]

    receipt = client.post(
        f"/api/agents/{agent_id}/messages/reconcile",
        json=body,
        headers=keyed_headers("key-multimodal-reconcile"),
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["inbound_id"] == sent.json()["inbound_id"]


# meiva:note
# The home runner's answer to a resurrection request: the effect the real op writes.
def _resurrecting_runner(
    db_conn: psycopg.Connection, agent_id: int
) -> Callable[[OpsCall], OpsReply]:

    def resurrect(call: OpsCall) -> OpsReply:
        assert call.payload["path"] == f"/api/agents/{agent_id}/resurrect-if-pending-work"
        assert call.payload["trigger_inbound_kind"] == "chat"
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status='idling' WHERE id=%s AND status='terminated'",
                (agent_id,),
            )
            if cur.rowcount == 1:
                cur.execute(
                    "INSERT INTO inbound_messages (agent_id,content,kind,source) "
                    "VALUES (%s,'','resurrect','system')",
                    (agent_id,),
                )
        db_conn.commit()
        return completed({"status": "spawned"})

    return resurrect


# meiva:note
# Make one post-commit step of message delivery fail until `broken` closes.
def _break_the_tail(
    step: str, client: GatewayTestClient, db_conn: psycopg.Connection, broken: ExitStack
) -> None:
    if step == "live-bus":
        working = client.app.state.bus
        client.app.state.bus = unpublishable_bus()
        broken.callback(setattr, client.app.state, "bus", working)
        return
    reply = (
        failed({"error": "RuntimeError: the runner's resurrect op crashed"})
        if step == "resurrection-fails"
        else completed({"status": "not-a-resurrect-result"})
    )
    broken.enter_context(runner_ops(db_conn, _HOME_RUNNER, {"lifecycle": reply}))


@pytest.mark.parametrize(
    "step", ["live-bus", "resurrection-fails", "resurrection-answer-malformed"]
)
def test_postcommit_error_response_preserves_receipt_and_same_key_recovery(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    agent_id: int,
    step: str,
) -> None:
    """Unknown tail failures fail this request, preserve its row and permit keyed healing."""
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    db_conn.commit()

    path = f"/api/agents/{agent_id}/messages"
    key = f"postcommit-{step}"
    body = {"content": "one logical message", "source": "user"}
    # The fixture already owns the real app lifespan. This client only
    # observes the HTTP error body instead of re-raising server exceptions.
    observer = GatewayTestClient(client.app, raise_server_exceptions=False)
    with ExitStack() as broken:
        broken.callback(observer.close)
        _break_the_tail(step, client, db_conn, broken)
        failed_response = observer.post(path, json=body, headers=keyed_headers(key))
        assert failed_response.status_code == 500, failed_response.text
        error = failed_response.json()
        assert error["committed"] is True
        assert error["idempotency_key"] == key
        assert error["retryable"] is False
        assert count_inbounds(db_conn, agent_id, body["content"]) == 1
        # Another request is still served by the same app after this bug.
        healthy = observer.get(f"/api/agents/{agent_id}")
        assert healthy.status_code == 200, healthy.text

    with runner_ops(db_conn, _HOME_RUNNER, {"lifecycle": _resurrecting_runner(db_conn, agent_id)}):
        healed = client.post(path + "/reconcile", json=body, headers=keyed_headers(key))
        assert healed.status_code == 200, healed.text
        assert healed.json()["inbound_id"] == error["inbound_id"]
        assert healed.json()["status"] == "idling"
        repeated = client.post(path, json=body, headers=keyed_headers(key))
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["inbound_id"] == error["inbound_id"]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT kind,count(*) FROM inbound_messages WHERE agent_id=%s GROUP BY kind ORDER BY kind",
            (agent_id,),
        )
        assert cur.fetchall() == [("chat", 1), ("resurrect", 1)]


# meiva:note
# The near-limit HTTP message commits first; its live announcement carries the whole text.
def test_large_message_is_announced_only_after_it_is_committed(
    client: GatewayTestClient,
    db_url: str,
    db_conn: psycopg.Connection,
    agent_id: int,
    event_bus: EventBus,
) -> None:
    content = "x" * (1024 * 1024 - 1)

    def committed_rows_at_announcement(payload: dict[str, object]) -> int:
        with psycopg.connect(db_url) as reader:
            return count_inbounds(reader, agent_id, content)

    with recording_live_events(event_bus, react=committed_rows_at_announcement) as live:
        response = client.post(
            f"/api/agents/{agent_id}/messages",
            json={"content": content, "source": "user"},
            headers=keyed_headers("large-awaited-live"),
        )
        assert response.status_code == 201, response.text
        (arrived,) = live.settle("inbound_arrived", 1)
        live.quiet()
        announced = live.events("inbound_arrived")
    assert [event.payload["inbound_id"] for event in announced] == [response.json()["inbound_id"]]
    assert arrived.payload["content"] == content
    assert arrived.reaction == 1
