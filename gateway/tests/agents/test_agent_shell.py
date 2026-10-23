from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    completed,
    failed,
    refused,
    runner_ops,
)


def _insert_agent(db: psycopg.Connection, *, machine: str = "unknown") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES ('t') RETURNING id", ())
        row = cur.fetchone()
        assert row is not None, "INSERT ... RETURNING must return a row"
        aid = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) "
            "VALUES (%s, 'user', 'running', %s)",
            (aid, machine),
        )
    return aid


def _capture(call: OpsCall) -> OpsReply:
    return completed(
        {
            "session_name": f"meiva-agent-{call.payload['agent_id']}-shell-3-watcher",
            "lines": ["line one", "line two"],
        }
    )


def test_shell_unknown_agent_404(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with gateway_test_client() as client:
        resp = client.get("/api/agents/999999/shell/0")
    assert resp.status_code == 404


def test_shell_no_such_session_404(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    db_conn.commit()
    missing = failed({"error": "ShellNotFoundError: agent 1 has no live shell 0 on this host"})

    with (
        runner_ops(db_conn, "runner-a", {"shell_capture": missing}),
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/agents/{aid}/shell/0")
    assert resp.status_code == 404
    assert "capture failed" in resp.json()["detail"]


def test_shell_capture_success(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    db_conn.commit()

    with (
        runner_ops(db_conn, "runner-a", {"shell_capture": _capture}) as runner,
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/agents/{aid}/shell/3")
    assert resp.status_code == 200
    assert resp.json() == {
        "agent_id": aid,
        "session_id": 3,
        "session_name": f"meiva-agent-{aid}-shell-3-watcher",
        "lines": ["line one", "line two"],
        "created_at": None,
        "uptime_seconds": 0,
        "expires_at": None,
        "renewals": 0,
        "last_renewed_at": None,
    }
    assert [(call.kind, call.payload) for call in runner.calls] == [
        ("shell_capture", {"agent_id": aid, "session_id": 3, "lines": 200})
    ]


def test_shell_capture_custom_lines_forwarded(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    db_conn.commit()

    with (
        runner_ops(db_conn, "runner-a", {"shell_capture": _capture}) as runner,
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/agents/{aid}/shell/1?lines=500")
    assert resp.status_code == 200
    assert runner.payloads("shell_capture") == [{"agent_id": aid, "session_id": 1, "lines": 500}]


def test_shell_capture_default_lines_follow_display_config(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    db_conn.commit()

    with (
        runner_ops(db_conn, "runner-a", {"shell_capture": _capture}) as runner,
        gateway_test_client(fields={"shell_capture_default_lines": 137}) as client,
    ):
        resp = client.get(f"/api/agents/{aid}/shell/1")
    assert resp.status_code == 200
    assert runner.payloads("shell_capture") == [{"agent_id": aid, "session_id": 1, "lines": 137}]


def test_shell_capture_carries_created_at_and_ttl_deadline(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    launched = datetime.now(tz=UTC) - timedelta(minutes=30)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) VALUES (%s, %s, %s)",
            (aid, 3, datetime.now(tz=UTC) + timedelta(hours=2)),
        )
    db_conn.commit()
    reply = completed(
        {
            "session_name": f"meiva-agent-{aid}-shell-3-dev",
            "lines": ["line one"],
            "created_at": launched.isoformat(),
            "uptime_seconds": 1800,
        }
    )

    with (
        runner_ops(db_conn, "runner-a", {"shell_capture": reply}),
        gateway_test_client() as client,
    ):
        body = client.get(f"/api/agents/{aid}/shell/3").json()
    assert body["created_at"] == launched.astimezone(UTC).isoformat().replace("+00:00", "Z")
    assert body["uptime_seconds"] == 1800
    assert body["expires_at"] is not None
    assert body["renewals"] == 0
    assert body["last_renewed_at"] is None


@pytest.mark.parametrize("name", ["page-preview", "schedule-check"])
def test_shell_capture_without_row_has_no_deadline(db_conn: psycopg.Connection, name: str) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    launched = datetime.now(tz=UTC) - timedelta(hours=3)
    db_conn.commit()
    reply = completed(
        {
            "session_name": f"meiva-agent-{aid}-shell-7-{name}",
            "lines": ["hello"],
            "created_at": launched.isoformat(),
            "uptime_seconds": 10800,
        }
    )

    with (
        runner_ops(db_conn, "runner-a", {"shell_capture": reply}),
        gateway_test_client() as client,
    ):
        body = client.get(f"/api/agents/{aid}/shell/7").json()
    assert datetime.fromisoformat(body["created_at"]) == launched
    assert body["expires_at"] is None
    assert body["renewals"] == 0
    assert body["last_renewed_at"] is None


def test_shell_capture_without_row_and_epoch_keeps_no_deadline(
    db_conn: psycopg.Connection,
) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    db_conn.commit()
    reply = completed(
        {
            "session_name": f"meiva-agent-{aid}-shell-8",
            "lines": ["hello"],
            "created_at": None,
            "uptime_seconds": 0,
        }
    )

    with (
        runner_ops(db_conn, "runner-a", {"shell_capture": reply}),
        gateway_test_client() as client,
    ):
        body = client.get(f"/api/agents/{aid}/shell/8").json()
    assert body["expires_at"] is None


def test_shell_capture_returns_recorded_deadline(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    launched = datetime.now(tz=UTC) - timedelta(minutes=5)
    deadline = datetime.now(tz=UTC) + timedelta(minutes=30)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) VALUES (%s, %s, %s)",
            (aid, 9, deadline),
        )
    db_conn.commit()
    reply = completed(
        {
            "session_name": f"meiva-agent-{aid}-shell-9",
            "lines": ["hello"],
            "created_at": launched.isoformat(),
            "uptime_seconds": 300,
        }
    )

    with (
        runner_ops(db_conn, "runner-a", {"shell_capture": reply}),
        gateway_test_client() as client,
    ):
        body = client.get(f"/api/agents/{aid}/shell/9").json()
    assert datetime.fromisoformat(body["expires_at"]) == deadline


def test_shell_machine_unreachable_503(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    db_conn.commit()

    with (
        runner_ops(db_conn, "runner-a", {"shell_capture": refused(404, "no ops here")}),
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/agents/{aid}/shell/1")
    assert resp.status_code == 503
    assert "unreachable" in resp.json()["detail"]


def test_shell_capture_carries_renewal_facts(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    renewed = datetime.now(tz=UTC) - timedelta(minutes=5)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, renewals, "
            "last_renewed_at) VALUES (%s, %s, %s, %s, %s)",
            (aid, 4, datetime.now(tz=UTC) + timedelta(hours=2), 3, renewed),
        )
    db_conn.commit()

    with (
        runner_ops(db_conn, "runner-a", {"shell_capture": _capture}),
        gateway_test_client() as client,
    ):
        body = client.get(f"/api/agents/{aid}/shell/4").json()
    assert body["renewals"] == 3
    assert body["last_renewed_at"] == renewed.astimezone(UTC).isoformat().replace("+00:00", "Z")
