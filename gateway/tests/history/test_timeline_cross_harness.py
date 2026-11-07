from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from base.agents.history.harness_messages import append_harness_timeline_items
from base.agents.history.timeline import TimelineItem
from base.config import settings
from base.db import create_agent
from gateway.tests.auth.gateway_config import gateway_test_client


@pytest.fixture
def test_client(db_conn: psycopg.Connection):
    with gateway_test_client() as client:
        yield client


def _put_checkpoint(agent_id: int, messages: list[Any]) -> None:
    ckpt = empty_checkpoint()
    ckpt["channel_values"] = {"messages": messages}
    ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=ckpt,
            metadata={"source": "input", "step": 1, "parents": {}},
            new_versions={"messages": "1"},
        )


def _set_harness(db_conn: psycopg.Connection, agent_id: int, harness: str) -> None:
    db_conn.execute(
        """
        INSERT INTO agents_meta (id, status, harness)
        VALUES (%s, 'idling', %s)
        ON CONFLICT (id) DO UPDATE SET harness = EXCLUDED.harness
        """,
        (agent_id, harness),
    )
    db_conn.commit()


def test_pure_meiva_agent_timeline(
    db_conn: psycopg.Connection,
    test_client: TestClient,
) -> None:
    tid = create_agent(db_conn)
    messages = [
        SystemMessage(content="You are a helpful assistant"),
        HumanMessage(content="Hello meiva"),
        AIMessage(content="Hello! How can I help?"),
    ]
    _put_checkpoint(tid, messages)

    resp = test_client.get(f"/api/agents/{tid}/timeline")
    assert resp.status_code == 200
    data = resp.json()
    assert data["msg_count"] == 3
    assert len(data["items"]) == 3
    assert [it["item_id"] for it in data["items"]] == ["0.0", "1.0", "2.0"]


def test_pure_external_harness_agent_timeline(
    db_conn: psycopg.Connection,
    test_client: TestClient,
) -> None:
    tid = create_agent(db_conn)
    _set_harness(db_conn, tid, "claude_code")
    items = [
        TimelineItem(
            item_id="h0.0",
            kind="inbound_chat",
            payload="Initial harness task",
            created_at="2026-05-01T10:00:00+00:00",
        ),
        TimelineItem(
            item_id="h1.0",
            kind="agent_chat",
            payload="Working on task",
            created_at="2026-05-01T10:01:00+00:00",
        ),
    ]
    append_harness_timeline_items(db_conn, tid, "claude_code", items)
    db_conn.commit()

    resp = test_client.get(f"/api/agents/{tid}/timeline")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert [it["item_id"] for it in data["items"]] == ["h0.0", "h1.0"]
    assert data["items"][0]["payload"] == "Initial harness task"
    assert data["items"][1]["payload"] == "Working on task"


def test_cross_harness_under_limit_merges_checkpoints(
    db_conn: psycopg.Connection,
    test_client: TestClient,
) -> None:
    tid = create_agent(db_conn)
    messages = [
        SystemMessage(content="System prompt"),
        HumanMessage(
            content="Meiva question",
            additional_kwargs={"meiva_created_at": "2026-05-01T09:00:00+00:00"},
        ),
        AIMessage(
            content="Meiva answer",
            additional_kwargs={"meiva_created_at": "2026-05-01T09:01:00+00:00"},
        ),
    ]
    _put_checkpoint(tid, messages)

    _set_harness(db_conn, tid, "codex")
    items = [
        TimelineItem(
            item_id="h0.0",
            kind="inbound_chat",
            payload="Codex follow-up",
            created_at="2026-05-01T09:02:00+00:00",
        ),
        TimelineItem(
            item_id="h1.0",
            kind="agent_chat",
            payload="Codex response",
            created_at="2026-05-01T09:03:00+00:00",
        ),
    ]
    append_harness_timeline_items(db_conn, tid, "claude_code", items)
    db_conn.commit()

    resp = test_client.get(f"/api/agents/{tid}/timeline?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 5
    item_ids = [it["item_id"] for it in data["items"]]
    assert item_ids == ["0.0", "1.0", "2.0", "h0.0", "h1.0"]
    assert data["items"][0]["kind"] == "system_prompt"
    assert data["items"][3]["payload"] == "Codex follow-up"
    assert data["items"][4]["payload"] == "Codex response"


def test_cross_harness_over_limit_avoids_checkpoint_deserialization(
    db_conn: psycopg.Connection,
    test_client: TestClient,
) -> None:
    tid = create_agent(db_conn)
    messages = [
        SystemMessage(content="System prompt"),
        HumanMessage(content="Old message"),
    ]
    _put_checkpoint(tid, messages)

    _set_harness(db_conn, tid, "claude_code")
    harness_items = [
        TimelineItem(
            item_id=f"h{i}.0",
            kind="agent_chat",
            payload=f"Harness turn {i}",
            created_at=f"2026-05-01T10:{i:02d}:00+00:00",
        )
        for i in range(10)
    ]
    append_harness_timeline_items(db_conn, tid, "claude_code", harness_items)
    db_conn.commit()

    resp = test_client.get(f"/api/agents/{tid}/timeline?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 5
    assert data["has_more"] is True
    assert [it["item_id"] for it in data["items"]] == ["h5.0", "h6.0", "h7.0", "h8.0", "h9.0"]


def test_cross_harness_paging_backward(
    db_conn: psycopg.Connection,
    test_client: TestClient,
) -> None:
    tid = create_agent(db_conn)
    messages = [
        SystemMessage(content="System prompt"),
        HumanMessage(
            content="Original prompt",
            additional_kwargs={"meiva_created_at": "2026-05-01T08:00:00+00:00"},
        ),
    ]
    _put_checkpoint(tid, messages)

    _set_harness(db_conn, tid, "antigravity")
    harness_items = [
        TimelineItem(
            item_id="h0.0",
            kind="inbound_chat",
            payload="Antigravity prompt",
            created_at="2026-05-01T09:00:00+00:00",
        ),
        TimelineItem(
            item_id="h1.0",
            kind="agent_chat",
            payload="Antigravity reply",
            created_at="2026-05-01T09:01:00+00:00",
        ),
    ]
    append_harness_timeline_items(db_conn, tid, "claude_code", harness_items)
    db_conn.commit()

    resp = test_client.get(f"/api/agents/{tid}/timeline?before=h0.0&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert [it["item_id"] for it in data["items"]] == ["0.0", "1.0"]
    assert data["has_more"] is False


def test_switched_agent_meiva_external_meiva_chronological(
    db_conn: psycopg.Connection,
    test_client: TestClient,
) -> None:
    tid = create_agent(db_conn)
    messages = [
        SystemMessage(content="System prompt"),
        HumanMessage(
            content="First meiva turn",
            additional_kwargs={"meiva_created_at": "2026-05-01T08:00:00+00:00"},
        ),
        AIMessage(
            content="Meiva reply after external",
            additional_kwargs={"meiva_created_at": "2026-05-01T10:00:00+00:00"},
        ),
    ]
    _put_checkpoint(tid, messages)

    harness_items = [
        TimelineItem(
            item_id="h0.0",
            kind="inbound_chat",
            payload="Harness middle turn",
            created_at="2026-05-01T09:00:00+00:00",
        ),
    ]
    append_harness_timeline_items(db_conn, tid, "claude_code", harness_items)
    db_conn.commit()

    _set_harness(db_conn, tid, "meiva")

    resp = test_client.get(f"/api/agents/{tid}/timeline?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 4
    assert [it["item_id"] for it in data["items"]] == ["0.0", "1.0", "h0.0", "2.0"]
