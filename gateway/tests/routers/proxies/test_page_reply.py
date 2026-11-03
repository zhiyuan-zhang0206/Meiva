# meiva:note
# The one write a served page may make: a bounded envelope rendered by the gateway into a
# chat message from the viewer. Task #6058.

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from base.db import create_agent
from gateway.tests.agents.gateway_auth import without_credentials
from gateway.tests.auth.gateway_config import gateway_test_client

# meiva:note
# The test client's bearer. Isolated to this module; no real credential is involved.
_SECRET = "page-reply-test-secret"  # noqa: S105


def _keyed_headers() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex, "Idempotency-Scope": "principal-v1"}


def _seed_agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute("INSERT INTO agents_meta (id, status) VALUES (%s, 'idling')", (agent_id,))
    conn.commit()
    return agent_id


def _open_page(client: Any, agent_id: int, name: str = "p") -> None:
    response = client.post(
        f"/api/keyed/v1/agents/{agent_id}/pages",
        json={"name": name, "port": 18080, "host": "127.0.0.1"},
        headers=_keyed_headers(),
    )
    assert response.status_code == 201, response.text


def _inbound_count(conn: psycopg.Connection, agent_id: int) -> int:
    row = conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _last_inbound(conn: psycopg.Connection, agent_id: int) -> tuple[object, ...]:
    row = conn.execute(
        "SELECT content, source, source_verified_by, source_transport "
        "FROM inbound_messages WHERE agent_id = %s ORDER BY id DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return row


def test_a_page_reply_reaches_the_agent_as_one_message(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    with gateway_test_client(cluster_secret=_SECRET) as client:
        _open_page(client, agent_id)
        response = client.post(
            f"/pages/{agent_id}-p/reply",
            json={"kind": "choice", "choices": ["Option A", "Option B"]},
        )
        assert response.status_code == 201, response.text
        assert response.json()["inbound_id"] is not None

    # meiva:note
    # The message reads the way the widget asked it: kind first, then the selected labels.
    # The source names the page; the auth fact is the viewer's own credential.
    assert _last_inbound(db_conn, agent_id) == (
        "choice: Option A, Option B",
        "ui:page:p",
        "cluster_bearer",
        "http",
    )


def test_a_note_lands_under_the_choice_line(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    with gateway_test_client(cluster_secret=_SECRET) as client:
        _open_page(client, agent_id)
        response = client.post(
            f"/pages/{agent_id}-p/reply",
            json={"kind": "confirm", "choices": ["approved"], "text": "looks right"},
        )
        assert response.status_code == 201, response.text
    assert _last_inbound(db_conn, agent_id)[0] == "confirm: approved\nlooks right"


def test_a_form_reply_carries_only_its_fields(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    with gateway_test_client(cluster_secret=_SECRET) as client:
        _open_page(client, agent_id)
        response = client.post(
            f"/pages/{agent_id}-p/reply",
            json={"kind": "form", "text": "name: Ada\nemail: ada@example.com"},
        )
        assert response.status_code == 201, response.text
    assert _last_inbound(db_conn, agent_id)[0] == "form:\nname: Ada\nemail: ada@example.com"


def test_a_page_reply_without_credentials_is_401(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    with gateway_test_client(cluster_secret=_SECRET) as client:
        _open_page(client, agent_id)
        response = without_credentials(client).post(
            f"/pages/{agent_id}-p/reply", json={"kind": "reply", "text": "hi"}
        )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        # meiva:note
        # Anything outside the envelope is refused rather than ignored: a page that grows a
        # field the route does not know is a page whose author expected it to matter.
        {"kind": "reply", "text": "hi", "to": "/api/agents/1/messages"},
        {"kind": "not-a-kind", "text": "hi"},
        {"kind": "reply"},
        {"kind": "reply", "text": "x" * 8_001},
        {"kind": "choice", "choices": ["x" * 201]},
        {"kind": "choice", "choices": ["ok"] * 51},
        {"kind": "choice", "choices": ["  "]},
    ],
)
def test_the_envelope_admits_nothing_outside_its_fields(
    db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    agent_id = _seed_agent(db_conn)
    with gateway_test_client(cluster_secret=_SECRET) as client:
        _open_page(client, agent_id)
        response = client.post(f"/pages/{agent_id}-p/reply", json=body)
    assert response.status_code == 422, response.text


def test_a_closed_or_unknown_page_is_404(db_conn: psycopg.Connection) -> None:
    agent_id = _seed_agent(db_conn)
    with gateway_test_client(cluster_secret=_SECRET) as client:
        never_opened = client.post(
            f"/pages/{agent_id}-missing/reply", json={"kind": "reply", "text": "hi"}
        )
        _open_page(client, agent_id)
        closed = client.post(
            f"/api/keyed/v1/agents/{agent_id}/pages/p/close",
            json={"expected_page_id": client.get(f"/api/agents/{agent_id}/pages").json()[0]["id"]},
            headers=_keyed_headers(),
        )
        assert closed.status_code == 200, closed.text
        after_close = client.post(
            f"/pages/{agent_id}-p/reply", json={"kind": "reply", "text": "hi"}
        )
    assert never_opened.status_code == 404
    assert after_close.status_code == 404


def test_the_recipient_comes_from_the_page_key_and_nowhere_else(
    db_conn: psycopg.Connection,
) -> None:
    # meiva:note
    # The crossing this exit closes: a page belongs to one agent, and only the server may say
    # which. A body that names another agent is refused as an unknown field, and even then the
    # refused call leaves no inbound for either agent.
    owner = _seed_agent(db_conn)
    other = _seed_agent(db_conn)
    with gateway_test_client(cluster_secret=_SECRET) as client:
        _open_page(client, owner)
        redirected = client.post(
            f"/pages/{owner}-p/reply",
            json={"kind": "reply", "text": "hi", "agent_id": other},
        )
        landed = client.post(f"/pages/{owner}-p/reply", json={"kind": "reply", "text": "hi"})

    assert redirected.status_code == 422, redirected.text
    assert landed.status_code == 201, landed.text
    assert _inbound_count(db_conn, other) == 0
    assert _inbound_count(db_conn, owner) == 1


def test_a_malformed_page_key_is_404(db_conn: psycopg.Connection) -> None:
    with gateway_test_client(cluster_secret=_SECRET) as client:
        response = client.post("/pages/not-a-key/reply", json={"kind": "reply", "text": "hi"})
    assert response.status_code == 404
