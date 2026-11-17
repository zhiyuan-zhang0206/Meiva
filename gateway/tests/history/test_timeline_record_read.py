# meiva:note
# The timeline of an agent whose CLI keeps its own record, as the page asks for it: the gateway asks the machine
# the agent lives on for a window of the record and serves what comes back. The test provides the one collaborator
# the route asks (the cluster client) and leaves the route itself untouched. A machine that cannot be reached is a
# 503, and a session with no record yet is an empty timeline.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any, Literal

import psycopg
import pytest
from starlette.requests import Request

from base.agents.history.timeline_item import TimelineItem
from base.db import create_agent
from gateway.agents.history.record_timeline import record_items_of
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from ops.cluster.rpc import ClusterOpUnreachable


class ScriptedRunner:
    def __init__(
        self,
        answer: dict[str, object] | Exception,
        *,
        on_dispatch: Callable[[], None] | None = None,
    ) -> None:
        self.answer = answer
        self.on_dispatch = on_dispatch
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def dispatch_to_machine(
        self,
        db: object,
        target_machine: str,
        kind: str,
        payload: dict[str, object],
        *,
        timeout_s: float | None = None,
        retries: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((target_machine, kind, payload))
        assert (timeout_s, retries) == (3.0, 0), "a short read, never retried"
        if self.on_dispatch is not None:
            self.on_dispatch()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture(params=["claude_code", "antigravity"])
def record_harness(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def client(db_url: str) -> Iterator[GatewayTestClient]:
    with gateway_test_client() as value:
        yield value


def _record_agent(conn: psycopg.Connection, harness: str) -> int:
    agent = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, machine, status, harness) VALUES (%s, 'macmini', 'idling', %s)",
        (agent, harness),
    )
    conn.commit()
    return agent


def _item(
    item_id: str, kind: Literal["inbound_chat", "agent_chat"], payload: str
) -> dict[str, Any]:
    return TimelineItem(
        item_id=item_id, kind=kind, payload=payload, created_at="2026-11-12T08:56:00+00:00"
    ).model_dump(mode="json")


def test_the_timeline_is_a_window_of_the_record_read_on_the_agents_machine(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    record_harness: str,
) -> None:
    agent = _record_agent(db_conn, record_harness)
    runner = ScriptedRunner(
        {
            "items": [_item("h413.0", "inbound_chat", "hi"), _item("h2620.0", "agent_chat", "yes")],
            "has_more": True,
            "size": 9800,
        }
    )
    client.app.state.rpc = runner

    body = client.get(f"/api/agents/{agent}/timeline").json()

    assert [item["item_id"] for item in body["items"]] == ["h413.0", "h2620.0"]
    assert (body["msg_count"], body["has_more"]) == (9800, True)
    [(machine, kind, payload)] = runner.calls
    assert (machine, kind) == ("macmini", "harness_timeline_read")
    assert payload["agent_id"] == agent and payload["harness"] == record_harness
    assert payload["before"] is None and isinstance(payload["limit"], int)


def test_a_page_before_an_item_asks_for_the_window_that_ends_at_its_row(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    record_harness: str,
) -> None:
    agent = _record_agent(db_conn, record_harness)
    runner = ScriptedRunner(
        {"items": [_item("h100.0", "agent_chat", "old")], "has_more": False, "size": 9800}
    )
    client.app.state.rpc = runner

    body = client.get(f"/api/agents/{agent}/timeline?before=h413.0&limit=7").json()

    assert [item["item_id"] for item in body["items"]] == ["h100.0"] and not body["has_more"]
    assert runner.calls[0][2]["before"] == 413 and runner.calls[0][2]["limit"] == 7


def test_an_unreachable_machine_is_a_503_and_not_an_empty_history(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    record_harness: str,
) -> None:
    agent = _record_agent(db_conn, record_harness)
    client.app.state.rpc = ScriptedRunner(ClusterOpUnreachable("the machine is down"))

    response = client.get(f"/api/agents/{agent}/timeline")

    assert response.status_code == 503


def test_a_session_with_no_record_is_an_empty_timeline(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    record_harness: str,
) -> None:
    agent = _record_agent(db_conn, record_harness)
    client.app.state.rpc = ScriptedRunner({"items": [], "has_more": False, "size": 0})

    response = client.get(f"/api/agents/{agent}/timeline")

    assert response.status_code == 200
    assert response.json() == {"items": [], "msg_count": 0, "has_more": False}


@pytest.mark.parametrize("before", ["bogus", "12.0", "s1.cp.3.0"])
def test_a_cursor_that_is_not_an_offset_id_is_answered_like_any_harness_and_asks_nothing(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    before: str,
    record_harness: str,
) -> None:
    agent = _record_agent(db_conn, record_harness)
    runner = ScriptedRunner({"items": [], "has_more": False, "size": 0})
    client.app.state.rpc = runner

    response = client.get(f"/api/agents/{agent}/timeline?before={before}")

    assert response.status_code == 200 and response.json()["items"] == []
    assert runner.calls == []


@pytest.mark.parametrize("record_harness", ["codex", "claude_code", "antigravity"])
@pytest.mark.parametrize(
    ("returned_offset", "expected_status"), [(413, 201), (414, 422), (None, 422)]
)
def test_quote_post_requires_the_exact_record_offset(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    record_harness: str,
    returned_offset: int | None,
    expected_status: int,
) -> None:
    agent = _record_agent(db_conn, record_harness)
    items = (
        []
        if returned_offset is None
        else [
            TimelineItem(
                item_id=f"h{returned_offset}.0",
                kind="agent_chat",
                payload="rendered selected text",
                source_message_id="record-message",
                source_block_idx=0,
            ).model_dump(mode="json")
        ]
    )
    runner = ScriptedRunner({"items": items, "has_more": False, "size": 9800})
    client.app.state.rpc = runner
    response = client.post(
        f"/api/agents/{agent}/messages",
        json={
            "content": "Explain this",
            "source": "user",
            "quote": {
                "source_agent_id": agent,
                "source_item_id": "h413.0",
                "source_message_id": "record-message",
                "source_block_idx": 0,
                "author": "Agent",
                "text": "selected text",
            },
        },
    )
    assert response.status_code == expected_status, response.text
    [(machine, kind, payload)] = runner.calls
    assert (machine, kind) == ("macmini", "harness_timeline_read")
    assert payload["agent_id"] == agent
    assert payload["harness"] == record_harness
    assert payload["before"] == 414
    assert payload["limit"] == 2
    row = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent,)
    ).fetchone()
    assert row is not None
    assert row[0] == (1 if expected_status == 201 else 0)


@pytest.mark.parametrize("harness", ["claude_code", "codex", "antigravity"])
@pytest.mark.parametrize("status", ["running", "idling", "terminated"])
def test_pending_record_activity_requires_a_running_agent(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    harness: str,
    status: str,
) -> None:
    agent = _record_agent(db_conn, harness)
    db_conn.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (status, agent))
    db_conn.commit()
    item = TimelineItem(
        item_id="h413.0",
        kind="agent_code",
        payload="Bash: interrupted command",
        created_at="2026-11-12T08:56:00+00:00",
        activity_pending=True,
    ).model_dump(mode="json")
    client.app.state.rpc = ScriptedRunner({"items": [item], "has_more": True, "size": 9800})

    response = client.get(f"/api/agents/{agent}/timeline")

    assert response.status_code == 200
    body = response.json()
    [shown] = body["items"]
    assert shown["activity_pending"] is (True if status == "running" else None)
    assert (shown["item_id"], shown["payload"]) == (item["item_id"], item["payload"])
    assert (body["has_more"], body["msg_count"]) == (True, 9800)


@pytest.mark.parametrize("harness", ["claude_code", "codex", "antigravity"])
@pytest.mark.parametrize("initial, current", [("idling", "running"), ("running", "idling")])
@pytest.mark.parametrize("consumer", ["head", "older", "snapshot", "items"])
def test_record_activity_uses_status_after_the_rpc_returns(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    harness: str,
    initial: str,
    current: str,
    consumer: str,
) -> None:
    agent = _record_agent(db_conn, harness)
    db_conn.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (initial, agent))
    db_conn.commit()

    def transition_during_rpc() -> None:
        db_conn.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (current, agent))
        db_conn.commit()

    item = TimelineItem(
        item_id="h100.0",
        kind="agent_code",
        payload="Bash: observed tool call",
        created_at="2026-11-12T08:56:00+00:00",
        activity_pending=True,
    ).model_dump(mode="json")
    runner = ScriptedRunner(
        {"items": [item], "has_more": True, "size": 9800}, on_dispatch=transition_during_rpc
    )
    client.app.state.rpc = runner

    if consumer == "items":
        request = Request({"type": "http", "app": client.app})
        [projected] = asyncio.run(record_items_of(request, agent))
        shown = projected.model_dump(mode="json")
    else:
        path = {
            "head": "timeline",
            "older": "timeline?before=h413.0&limit=7",
            "snapshot": "conversation-snapshot",
        }[consumer]
        response = client.get(f"/api/agents/{agent}/{path}")
        assert response.status_code == 200
        body = response.json()
        if consumer == "snapshot":
            body = body["timeline"]
        [shown] = body["items"]
        assert (body["has_more"], body["msg_count"]) == (True, 9800)
    assert shown["activity_pending"] is (True if current == "running" else None)
    assert (shown["item_id"], shown["payload"]) == (item["item_id"], item["payload"])
    [(_, kind, payload)] = runner.calls
    assert kind == "harness_timeline_read"
    assert payload["before"] == (413 if consumer == "older" else None)
    if consumer == "older":
        assert payload["limit"] == 7
