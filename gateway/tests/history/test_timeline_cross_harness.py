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
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.history.test_timeline_record_read import ScriptedRunner


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


def test_compact_excludes_pre_compact_harness_items(
    db_conn: psycopg.Connection,
    test_client: TestClient,
) -> None:
    tid = create_agent(db_conn)
    pre_compact_harness = [
        TimelineItem(
            item_id="h0.0",
            kind="system_marker",
            payload="error: Harness execution stopped: antigravity turn failed",
            created_at="2026-05-01T08:00:00+00:00",
        ),
    ]
    append_harness_timeline_items(db_conn, tid, "antigravity", pre_compact_harness)
    db_conn.commit()

    messages = [
        SystemMessage(content="System prompt"),
        HumanMessage(
            content="Compact summary [2026-05-01]: context compacted",
            additional_kwargs={
                "meiva_msg_type": "compact_summary",
                "meiva_created_at": "2026-05-01T12:00:00+00:00",
            },
        ),
        AIMessage(
            content="Active post-compact assistant turn",
            additional_kwargs={"meiva_created_at": "2026-05-01T12:05:00+00:00"},
        ),
    ]
    _put_checkpoint(tid, messages)
    _set_harness(db_conn, tid, "meiva")

    resp = test_client.get(f"/api/agents/{tid}/timeline?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 3
    assert [it["item_id"] for it in data["items"]] == ["0.0", "1.0", "2.0"]
    assert all(it["item_id"] != "h0.0" for it in data["items"])


# meiva:note
# The current record owns both pages, even when older stored histories exceed the limit.
@pytest.mark.parametrize("checkpoint_count", [0, 3, 25])
@pytest.mark.parametrize("limit", [2, 10])
def test_current_antigravity_session_excludes_previous_harness_history(
    db_conn: psycopg.Connection, test_client: GatewayTestClient, checkpoint_count: int, limit: int
) -> None:
    agent = create_agent(db_conn)
    if checkpoint_count:
        _put_checkpoint(
            agent,
            [
                SystemMessage(content="old system"),
                *[AIMessage(content=f"old checkpoint {i}") for i in range(checkpoint_count)],
            ],
        )
    append_harness_timeline_items(
        db_conn,
        agent,
        "antigravity",
        [
            TimelineItem(item_id=f"h{i}.0", kind="agent_chat", payload=f"old stored row {i}")
            for i in range(25)
        ],
    )
    _set_harness(db_conn, agent, "antigravity")
    db_conn.execute("UPDATE agents_meta SET machine='macmini' WHERE id=%s", (agent,))
    db_conn.commit()
    rows = [
        TimelineItem(item_id="h800.0", kind="agent_chat", payload="current CLI reply").model_dump(
            mode="json"
        )
    ]
    runner = ScriptedRunner({"items": rows, "has_more": True, "size": 1200})
    test_client.app.state.rpc = runner
    response = test_client.get(f"/api/agents/{agent}/timeline?limit={limit}")
    assert response.status_code == 200
    assert response.json() == {"items": rows, "msg_count": 1200, "has_more": True}
    assert runner.calls[-1][2]["limit"] == limit
    older: dict[str, object] = {"items": [], "has_more": False, "size": 1200}
    runner.answer = older
    page = test_client.get(f"/api/agents/{agent}/timeline?before=h800.0&limit={limit}")
    assert page.status_code == 200
    assert page.json() == {"items": [], "msg_count": 1200, "has_more": False}
    assert runner.calls[-1][2]["before"] == 800
