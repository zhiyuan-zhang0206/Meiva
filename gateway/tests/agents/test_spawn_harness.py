from __future__ import annotations

import psycopg
from fastapi.testclient import TestClient

from tests.fixtures.gateway_config import gateway_test_client, spawn_via_http


def _checkpoint(conn: psycopg.Connection, agent_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, "
            "checkpoint, metadata) VALUES (%s, '', 'ckpt-1', 'x', '{}'::jsonb, '{}'::jsonb)",
            (str(agent_id),),
        )
    conn.commit()
    return "ckpt-1"


def _harness(conn: psycopg.Connection, agent_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT harness FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _agent_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agents")
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _newest_agent(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT max(id) FROM agents")
        row = cur.fetchone()
    assert row is not None and row[0] is not None
    return int(row[0])


def _refused_launch(client: TestClient, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    response = spawn_via_http(client, {"spawner": "user", **body})
    assert response.status_code == 502, response.text
    receipt = response.json()
    assert receipt["reason"] == "agent_launch_failed"
    return int(receipt["agent_id"]), receipt


class TestSpawnHarness:
    def test_spawn_records_the_requested_harness(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent, _ = _refused_launch(client, {"harness": "codex"})
        assert _harness(db_conn, agent) == "codex"

    def test_spawn_without_a_harness_runs_the_builtin_runtime(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent = spawn_via_http(client, {"spawner": "user"}).json()["id"]
        assert _harness(db_conn, agent) == "meiva"

    def test_unknown_harness_is_rejected_without_creating_an_agent(
        self, db_conn: psycopg.Connection
    ) -> None:
        before = _agent_count(db_conn)
        with gateway_test_client() as client:
            response = spawn_via_http(client, {"spawner": "user", "harness": "claude-code"})
        assert response.status_code == 422
        assert _agent_count(db_conn) == before

    def test_fork_inherits_the_source_harness_through_http(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            source, _ = _refused_launch(client, {"harness": "antigravity"})
            _checkpoint(db_conn, source)
            fork, _ = _refused_launch(client, {"fork_from": source})
        assert _harness(db_conn, source) == "antigravity"
        assert _harness(db_conn, fork) == "antigravity"

    def test_fork_naming_another_harness_is_rejected(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            source = spawn_via_http(client, {"spawner": "user"}).json()["id"]
            _checkpoint(db_conn, source)
            response = spawn_via_http(
                client,
                {"spawner": "user", "fork_from": source, "harness": "claude_code"},
            )
        assert response.status_code == 400
        body = response.json()
        assert body["reason"] == "fork_harness_mismatch"
        assert "immutable" in body["detail"]

    def test_fork_naming_its_own_harness_is_not_refused(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            source, _ = _refused_launch(client, {"harness": "codex"})
            _checkpoint(db_conn, source)
            fork, _ = _refused_launch(client, {"fork_from": source, "harness": "codex"})
        assert _harness(db_conn, fork) == "codex"
