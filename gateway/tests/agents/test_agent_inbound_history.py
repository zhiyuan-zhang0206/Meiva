# meiva:note
# GET /api/agents/{id}/inbound — one agent's inbound queue as bounded history, against rows in the
# real table: every status and kind is visible (not only what is queued), newest first, the
# filters compose, the content is a preview unless `full` is asked for, the window and the limit
# bound the read, another agent's rows never appear, and the 404 / 422 / 503 mappings.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import psycopg
import pytest

from base.config.tests.configuration import snapshot_process_config
from base.db import create_agent
from gateway.http.config import GatewayConfig
from gateway.tests.auth.gateway_config import gateway_config_of, gateway_test_client


# meiva:note
# Commit one inbound row `minutes_ago` minutes old and return its id.
def _inbound(
    db: psycopg.Connection,
    agent_id: int,
    content: str,
    *,
    minutes_ago: float = 1,
    kind: str = "chat",
    source: str = "user",
    status: str = "pending",
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, status, created_at, "
            "claimed_at) VALUES (%s, %s, %s, %s, %s, now() - make_interval(mins => %s), "
            "CASE WHEN %s = 'pending' THEN NULL ELSE now() END) RETURNING id",
            (agent_id, content, kind, source, status, minutes_ago, status),
        )
        row = cur.fetchone()
        assert row is not None
        inbound_id: int = row[0]
    db.commit()
    return inbound_id


def _get(agent_id: int, **params: Any) -> dict[str, Any]:
    with gateway_test_client() as client:
        response = client.get(f"/api/agents/{agent_id}/inbound", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


@pytest.fixture
def agent(db_conn: psycopg.Connection) -> int:
    return create_agent(db_conn)


def test_every_status_is_listed_newest_first(db_conn: psycopg.Connection, agent: int) -> None:
    oldest = _inbound(db_conn, agent, "a", minutes_ago=30, status="done")
    middle = _inbound(db_conn, agent, "b", minutes_ago=20, status="claimed")
    newest = _inbound(db_conn, agent, "c", minutes_ago=10, status="pending")

    body = _get(agent)

    assert _ids(body) == [newest, middle, oldest]
    assert [item["status"] for item in body["items"]] == ["pending", "claimed", "done"]
    assert body["agent_id"] == agent
    assert body["meta"]["has_more"] is False
    first = body["items"][0]
    assert set(first) == {
        "id",
        "kind",
        "source",
        "status",
        "created_at",
        "claimed_at",
        "content",
        "content_truncated",
    }
    assert first["claimed_at"] is None
    assert body["items"][1]["claimed_at"] is not None


def test_filters_compose(db_conn: psycopg.Connection, agent: int) -> None:
    chat = _inbound(db_conn, agent, "hi", source="agent:12", status="claimed")
    _inbound(db_conn, agent, "hi2", source="user", status="claimed")
    _inbound(db_conn, agent, "note", kind="system_note", source="agent:12", status="claimed")
    _inbound(db_conn, agent, "hi3", source="agent:12", status="pending")

    assert _ids(_get(agent, status="claimed", kind="chat", source="agent:12")) == [chat]
    assert len(_ids(_get(agent, source="agent:12"))) == 3
    assert len(_ids(_get(agent, kind="system_note"))) == 1


def test_only_this_agents_rows_are_read(db_conn: psycopg.Connection, agent: int) -> None:
    other = create_agent(db_conn)
    mine = _inbound(db_conn, agent, "mine")
    _inbound(db_conn, other, "theirs")
    assert _ids(_get(agent)) == [mine]


def test_content_is_a_preview_unless_full_is_asked_for(
    db_conn: psycopg.Connection, agent: int
) -> None:
    short = _inbound(db_conn, agent, "short", minutes_ago=2)
    long_text = "x" * 500
    long = _inbound(db_conn, agent, long_text, minutes_ago=1)

    preview = {item["id"]: item for item in _get(agent)["items"]}
    assert preview[short]["content"] == "short"
    assert preview[short]["content_truncated"] is False
    assert preview[long]["content"] == "x" * 200
    assert preview[long]["content_truncated"] is True

    whole = {item["id"]: item for item in _get(agent, full="true")["items"]}
    assert whole[long]["content"] == long_text
    assert whole[long]["content_truncated"] is False


def test_the_window_defaults_to_a_day_and_hours_narrows_it(
    db_conn: psycopg.Connection, agent: int
) -> None:
    old = _inbound(db_conn, agent, "old", minutes_ago=3 * 60)
    recent = _inbound(db_conn, agent, "recent", minutes_ago=5)
    _inbound(db_conn, agent, "ancient", minutes_ago=3 * 24 * 60)

    assert _ids(_get(agent)) == [recent, old]
    assert _ids(_get(agent, hours=1)) == [recent]
    assert _get(agent)["meta"]["window_from"] is not None


def test_limit_bounds_the_page_and_reports_older_rows(
    db_conn: psycopg.Connection, agent: int
) -> None:
    for index in range(3):
        _inbound(db_conn, agent, f"m{index}", minutes_ago=10 - index)

    body = _get(agent, limit=2)

    assert len(body["items"]) == 2
    assert body["meta"]["has_more"] is True
    assert body["meta"]["limit"] == 2


def test_the_default_limit_is_the_configured_message_window(
    db_conn: psycopg.Connection, agent: int
) -> None:
    for index in range(3):
        _inbound(db_conn, agent, f"m{index}", minutes_ago=10 - index)
    with gateway_test_client(fields={"messages_default_limit": 2}) as client:
        body = client.get(f"/api/agents/{agent}/inbound").json()
    assert len(body["items"]) == 2
    assert body["meta"]["limit"] == 2


def test_an_unknown_agent_is_404(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        assert client.get("/api/agents/999999/inbound").status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"status": "lost"},
        {"kind": "gossip"},
        {"source": ""},
        {"hours": 0},
        {"hours": 9000},
        {"limit": 0},
        {"limit": 1001},
    ],
)
def test_a_value_outside_the_vocabulary_is_422(
    db_conn: psycopg.Connection, agent: int, params: dict[str, Any]
) -> None:
    with gateway_test_client() as client:
        assert client.get(f"/api/agents/{agent}/inbound", params=params).status_code == 422


# meiva:note
# Hold the inbound table exclusively, so a read of it waits for the lock.
@contextmanager
def _inbound_table_locked(db_url: str) -> Generator[None]:
    with psycopg.connect(db_url) as holder:
        holder.execute("LOCK TABLE inbound_messages IN ACCESS EXCLUSIVE MODE")
        try:
            yield
        finally:
            holder.rollback()


def test_a_read_past_its_statement_timeout_is_503(db_url: str, db_conn: psycopg.Connection) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    config: GatewayConfig = replace(
        gateway_config_of(snapshot_process_config()),
        human_auth="required",
        events_read_timeout_ms=150,
    )
    with _inbound_table_locked(db_url), gateway_test_client(config=config) as client:
        response = client.get(f"/api/agents/{agent_id}/inbound")
    assert response.status_code == 503
    assert response.json()["detail"] == "inbound read timed out"
