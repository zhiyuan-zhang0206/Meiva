# meiva:note
# The timeline of an agent whose CLI keeps its own record, as the page asks for it: the gateway asks the machine
# the agent lives on for a window of the record and serves what comes back. The test provides the one collaborator
# the route asks (the cluster client) and leaves the route itself untouched. A machine that cannot be reached is a
# 503, a session with no record yet is an empty timeline, and the rows an earlier scheme stored are never read.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Literal

import psycopg
import pytest

from base.agents.history.harness_messages import append_harness_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.db import create_agent
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from ops.cluster.rpc import ClusterOpUnreachable


class ScriptedRunner:
    def __init__(self, answer: dict[str, object] | Exception) -> None:
        self.answer = answer
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
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def client(db_url: str) -> Iterator[GatewayTestClient]:
    with gateway_test_client() as value:
        yield value


def _claude_agent(conn: psycopg.Connection) -> int:
    agent = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, machine, status, harness) VALUES (%s, 'macmini', 'idling', 'claude_code')",
        (agent,),
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
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _claude_agent(db_conn)
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
    assert payload["agent_id"] == agent and payload["harness"] == "claude_code"
    assert payload["before"] is None and isinstance(payload["limit"], int)


def test_a_page_before_an_item_asks_for_the_window_that_ends_at_its_row(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _claude_agent(db_conn)
    runner = ScriptedRunner(
        {"items": [_item("h100.0", "agent_chat", "old")], "has_more": False, "size": 9800}
    )
    client.app.state.rpc = runner

    body = client.get(f"/api/agents/{agent}/timeline?before=h413.0&limit=7").json()

    assert [item["item_id"] for item in body["items"]] == ["h100.0"] and not body["has_more"]
    assert runner.calls[0][2]["before"] == 413 and runner.calls[0][2]["limit"] == 7


def test_an_unreachable_machine_is_a_503_and_not_an_empty_history(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _claude_agent(db_conn)
    client.app.state.rpc = ScriptedRunner(ClusterOpUnreachable("the machine is down"))

    response = client.get(f"/api/agents/{agent}/timeline")

    assert response.status_code == 503


def test_a_session_with_no_record_is_an_empty_timeline_and_old_rows_are_never_read(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _claude_agent(db_conn)
    append_harness_timeline_items(
        db_conn,
        agent,
        "claude_code",
        [TimelineItem(item_id="0.0", kind="agent_chat", payload="stale row from the old scheme")],
    )
    db_conn.commit()
    client.app.state.rpc = ScriptedRunner({"items": [], "has_more": False, "size": 0})

    response = client.get(f"/api/agents/{agent}/timeline")

    assert response.status_code == 200
    assert response.json() == {"items": [], "msg_count": 0, "has_more": False}
