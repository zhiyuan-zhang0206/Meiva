# meiva:note
# The timeline of a Codex agent, as the page asks for it: the same window of the rollout read on the agent's machine
# that a Claude agent gets (`test_timeline_record_read.py` states the rules; here only what is Codex's): the operation
# names the harness, an unreachable machine is a 503, a thread with no rollout yet is an empty timeline, and the rows an
# earlier scheme stored are never read.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from base.agents.history.harness_messages import append_harness_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.db import create_agent
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.history.test_timeline_record_read import ScriptedRunner
from ops.cluster.rpc import ClusterOpUnreachable


@pytest.fixture
def client(db_url: str) -> Iterator[GatewayTestClient]:
    with gateway_test_client() as value:
        yield value


def _codex_agent(conn: psycopg.Connection) -> int:
    agent = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, machine, status, harness) VALUES (%s, 'macmini', 'idling', 'codex')",
        (agent,),
    )
    conn.commit()
    return agent


def test_the_window_is_read_on_the_agents_machine_under_the_codex_harness(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _codex_agent(db_conn)
    item = TimelineItem(
        item_id="h4360.0",
        kind="inbound_chat",
        payload="hi",
        created_at="2026-11-12T19:45:25+00:00",
    ).model_dump(mode="json")
    runner = ScriptedRunner({"items": [item], "has_more": False, "size": 68233})
    client.app.state.rpc = runner

    body = client.get(f"/api/agents/{agent}/timeline").json()

    assert [i["item_id"] for i in body["items"]] == ["h4360.0"] and body["msg_count"] == 68233
    [(machine, kind, payload)] = runner.calls
    assert (machine, kind, payload["harness"]) == ("macmini", "harness_timeline_read", "codex")


def test_an_unreachable_machine_is_a_503_and_not_an_empty_history(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _codex_agent(db_conn)
    client.app.state.rpc = ScriptedRunner(ClusterOpUnreachable("the machine is down"))

    assert client.get(f"/api/agents/{agent}/timeline").status_code == 503


def test_a_thread_with_no_rollout_is_an_empty_timeline_and_old_rows_are_never_read(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _codex_agent(db_conn)
    append_harness_timeline_items(
        db_conn,
        agent,
        "codex",
        [TimelineItem(item_id="0.0", kind="agent_chat", payload="stale row from the old scheme")],
    )
    db_conn.commit()
    client.app.state.rpc = ScriptedRunner({"items": [], "has_more": False, "size": 0})

    response = client.get(f"/api/agents/{agent}/timeline")

    assert response.status_code == 200
    assert response.json() == {"items": [], "msg_count": 0, "has_more": False}
