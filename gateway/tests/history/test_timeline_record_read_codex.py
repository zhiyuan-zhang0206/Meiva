# meiva:note
# The timeline of a Codex agent, as the page asks for it: the same window of the rollout read on the agent's machine
# that a Claude agent gets (`test_timeline_record_read.py` states the rules; here only what is Codex's): the operation
# names the harness, an unreachable machine is a 503, and a thread with no rollout yet is an empty timeline.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.history.timeline_item import TimelineItem
from base.db import create_agent
from base.lm.harness.registry import FileSessionRegistry, SessionRecord
from base.paths import harness_sessions_dir
from gateway.agents.history.timeline import TimelineResponse
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.history.test_timeline_record_read import ScriptedRunner
from ops.cluster.rpc import ClusterOpUnreachable
from ops.harness_timeline import read_harness_timeline
from ops.rpc_schemas import HarnessTimelineReadRequest


@pytest.fixture
def client(db_url: str) -> Iterator[GatewayTestClient]:
    with gateway_test_client() as value:
        yield value


def _codex_agent(conn: psycopg.Connection) -> int:
    agent = create_agent(conn)
    _ = conn.execute(
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

    body = TimelineResponse.model_validate_json(client.get(f"/api/agents/{agent}/timeline").text)

    assert [i.item_id for i in body.items] == ["h4360.0"] and body.msg_count == 68233
    [(machine, kind, payload)] = runner.calls
    assert (machine, kind, payload["harness"]) == ("macmini", "harness_timeline_read", "codex")


def test_an_unreachable_machine_is_a_503_and_not_an_empty_history(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _codex_agent(db_conn)
    client.app.state.rpc = ScriptedRunner(ClusterOpUnreachable("the machine is down"))

    assert client.get(f"/api/agents/{agent}/timeline").status_code == 503


def test_a_thread_with_no_rollout_is_an_empty_timeline(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent = _codex_agent(db_conn)
    client.app.state.rpc = ScriptedRunner({"items": [], "has_more": False, "size": 0})

    response = client.get(f"/api/agents/{agent}/timeline")

    assert response.status_code == 200
    assert response.json() == {"items": [], "msg_count": 0, "has_more": False}


class RecordOperationRunner:
    def __init__(
        self,
        home: Path,
        environ: dict[str, str],
        pool: ConnectionPool,
        *,
        on_dispatch: Callable[[], None] | None = None,
    ) -> None:
        self.home, self.environ, self.pool = home, environ, pool
        self.on_dispatch = on_dispatch
        self.raw_pending = False

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
        if kind == "lifecycle":
            assert str(payload["path"]).endswith("/resurrect-if-pending-work")
            return {"status": "spawned"}
        assert target_machine == "macmini" and kind == "harness_timeline_read"
        assert (timeout_s, retries) == (3.0, 0)
        result = await asyncio.to_thread(
            read_harness_timeline,
            HarnessTimelineReadRequest.model_validate(payload),
            home=self.home,
            environ=self.environ,
            pool=self.pool,
        )
        self.raw_pending = any(item.activity_pending for item in result.items)
        if self.on_dispatch is not None:
            self.on_dispatch()
        return result.model_dump(mode="json")


@pytest.mark.parametrize("source_kind", ["agent_chat", "agent_code", "code_output"])
def test_quote_http_reads_the_actual_complete_record_row(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
    tmp_path: Path,
    source_kind: str,
) -> None:
    agent = _codex_agent(db_conn)
    session = "01a1d14b-0f88-7ee3-8636-d3b8c1507a76"
    home = tmp_path / "home"
    FileSessionRegistry(harness_sessions_dir(home=home)).save(
        SessionRecord(
            agent_id=agent,
            harness="codex",
            session_id=session,
            cwd=str(tmp_path),
            model="m",
            created_at="2026-11-12T00:00:00Z",
            updated_at="2026-11-12T00:00:00Z",
            generation=1,
        )
    )
    fixture = (
        Path(__file__).parents[3]
        / "base/lm/harness/codex/transcript/tests/fixtures/rollout/session.jsonl"
    )
    record = tmp_path / "codex/sessions/2026/11/13" / f"rollout-2026-11-13T03-45-19-{session}.jsonl"
    record.parent.mkdir(parents=True)
    raw = fixture.read_bytes()
    _ = record.write_bytes(raw)
    pool: ConnectionPool = ConnectionPool(db_url, min_size=1, max_size=1)
    with pool:
        client.app.state.rpc = RecordOperationRunner(
            home, {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "codex")}, pool
        )
        response = client.get(f"/api/agents/{agent}/timeline")
        assert response.status_code == 200, response.text
        head = TimelineResponse.model_validate_json(response.text)
        target = next(item for item in head.items if item.kind == source_kind)
        assert not target.activity_pending
        offset = int(target.item_id.split(".")[0][1:])
        assert raw.rfind(b"\n", 0, offset) + 1 == offset
        assert raw.find(b"\n", offset) + 1 > offset + 1
        quote: dict[str, object] = {
            "source_agent_id": agent,
            "source_item_id": target.item_id,
            "author": "Agent",
            "text": str(target.payload).strip()[:80],
        }
        body: dict[str, object] = {"content": "Explain this", "source": "user", "quote": quote}
        accepted = client.post(f"/api/agents/{agent}/messages", json=body)
        assert accepted.status_code == 201, accepted.text
        quote["source_item_id"] = f"h{offset + 1}.0"
        rejected = client.post(f"/api/agents/{agent}/messages", json=body)
        assert rejected.status_code == 422, rejected.text
        quote["source_item_id"] = target.item_id
        FileSessionRegistry(harness_sessions_dir(home=home)).save(
            SessionRecord(
                agent_id=agent,
                harness="codex",
                session_id="replacement-session",
                cwd=str(tmp_path),
                model="m",
                created_at="2026-11-12T00:00:00Z",
                updated_at="2026-11-12T00:01:00Z",
                generation=2,
            )
        )
        assert record.exists()
        replaced = client.post(f"/api/agents/{agent}/messages", json=body)
        assert replaced.status_code == 422, replaced.text
    row = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone()
    assert row is not None and row[0] == 1


@pytest.mark.parametrize(
    "initial, final",
    [
        ("idling", "idling"),
        ("terminated", "terminated"),
        ("running", "running"),
        ("idling", "running"),
        ("running", "idling"),
    ],
)
@pytest.mark.parametrize("partial", ["none", "line", "text"])
def test_quote_record_activity_parity(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
    tmp_path: Path,
    initial: str,
    final: str,
    partial: str,
) -> None:
    agent = _codex_agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET harness='claude_code' WHERE id=%s", (agent,))
    db_conn.commit()
    session = "parity-session"
    home = tmp_path / "home"
    FileSessionRegistry(harness_sessions_dir(home=home)).save(
        SessionRecord(
            agent_id=agent,
            harness="claude_code",
            session_id=session,
            cwd=str(tmp_path),
            model="m",
            created_at="2026-11-18T00:00:00Z",
            updated_at="2026-11-18T00:00:00Z",
            generation=1,
        )
    )
    record = tmp_path / ".claude/projects/test" / f"{session}.jsonl"
    record.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "timestamp": "2026-11-18T00:00:00Z",
            "type": "assistant",
            "uuid": "parity-message",
            "isAbortedMidStream": partial == "text",
            "message": {
                "id": "parity-message",
                "content": [
                    {
                        "type": "text" if partial == "text" else "tool_use",
                        "text": "unfinished text",
                        "id": "call-parity",
                        "name": "Bash",
                        "input": {"command": "echo complete"},
                    }
                ],
            },
        }
    ).encode()
    _ = record.write_bytes(raw + (b"" if partial == "line" else b"\n"))

    def status(value: str) -> None:
        db_conn.execute("UPDATE agents_meta SET status=%s WHERE id=%s", (value, agent))
        db_conn.commit()

    status(final)
    pool: ConnectionPool = ConnectionPool(db_url, min_size=1, max_size=1)
    with pool:
        runner = RecordOperationRunner(
            home, {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}, pool
        )
        client.app.state.rpc = runner
        timeline = client.get(f"/api/agents/{agent}/timeline")
        assert timeline.status_code == 200, timeline.text
        if partial == "none":
            assert runner.raw_pending
            assert timeline.json()["items"][0]["activity_pending"] is (
                True if final == "running" else None
            )
        status(initial)
        runner.on_dispatch = lambda: status(final)
        response = client.post(
            f"/api/agents/{agent}/messages",
            json={
                "content": "Explain",
                "source": "user",
                "quote": {
                    "source_agent_id": agent,
                    "source_item_id": "h0.0",
                    "author": "Agent",
                    "text": "echo complete",
                },
            },
        )
        accepted = partial == "none" and final != "running"
        assert response.status_code == (201 if accepted else 422), response.text
    row = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone()
    assert row is not None and row[0] == int(accepted)


@pytest.mark.parametrize("agent_status", ["idling", "terminated"])
@pytest.mark.parametrize("step_status", ["GENERATING", "RUNNING", "DONE"])
def test_quote_rejects_incomplete_antigravity_planner_text(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
    tmp_path: Path,
    agent_status: str,
    step_status: str,
) -> None:
    agent = _codex_agent(db_conn)
    _ = db_conn.execute(
        "UPDATE agents_meta SET harness='antigravity', status=%s WHERE id=%s", (agent_status, agent)
    )
    db_conn.commit()
    home = tmp_path / "home"
    FileSessionRegistry(harness_sessions_dir(home=home)).save(
        SessionRecord(
            agent_id=agent,
            harness="antigravity",
            session_id="planner",
            cwd=str(tmp_path),
            model="m",
            created_at="2026-11-18T00:00:00Z",
            updated_at="2026-11-18T00:00:00Z",
            generation=1,
        )
    )
    record = (
        tmp_path
        / ".gemini/antigravity-cli/brain/planner/.system_generated/logs/transcript_full.jsonl"
    )
    record.parent.mkdir(parents=True)
    _ = record.write_text(
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "status": step_status,
                "created_at": "2026-11-18T00:00:00Z",
                "content": "planner text",
                "tool_calls": [],
            }
        )
        + "\n"
    )
    pool: ConnectionPool = ConnectionPool(db_url, min_size=1, max_size=1)
    with pool:
        client.app.state.rpc = RecordOperationRunner(home, {"HOME": str(tmp_path)}, pool)
        response = client.post(
            f"/api/agents/{agent}/messages",
            json={
                "content": "Explain",
                "source": "user",
                "quote": {
                    "source_agent_id": agent,
                    "source_item_id": "h0.0",
                    "author": "Agent",
                    "text": "planner text",
                },
            },
        )
        assert response.status_code == (201 if step_status == "DONE" else 422), response.text
    row = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone()
    assert row is not None and row[0] == int(step_status == "DONE")
