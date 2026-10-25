# meiva:note
# `GET /api/commands`: the composer catalog, globally or as one agent's runner sees it.
#
# The catalog scans real command files under the unit home; an agent view is answered by the
# agent's runner over a real listener (`runner_ops`), and the gateway-local catalog is the
# fallback when that runner is absent or failing.

from pathlib import Path

import psycopg
import pytest

from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.support.runner_ops.ops_stub import (
    OpsReply,
    completed,
    expecting,
    failed,
    refused,
    runner_ops,
)

LOCAL_COMMAND = {"name": "recap", "description": "d", "instruction_hint": "h"}


@pytest.fixture
def local_command(unit_home: Path) -> dict[str, str]:
    """A command file in the unit's own `commands/` directory."""
    commands = unit_home / "commands"
    commands.mkdir(exist_ok=True)
    (commands / "recap.md").write_text(
        "---\ndescription: d\ninstruction-hint: h\n---\nbody\n", encoding="utf-8"
    )
    return LOCAL_COMMAND


def _insert_agent(db: psycopg.Connection, machine: str, *, harness: str = "meiva") -> int:
    row = db.execute("INSERT INTO agents (label) VALUES ('t') RETURNING id").fetchone()
    assert row is not None
    db.execute(
        "INSERT INTO agents_meta (id, spawner, status, machine, harness) "
        "VALUES (%s, 'user', 'idling', %s, %s)",
        (row[0], machine, harness),
    )
    db.commit()
    return row[0]


def test_endpoint_returns_command_metadata(local_command: dict[str, str]) -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/commands")
    assert resp.status_code == 200, resp.text
    assert local_command in resp.json()


def test_endpoint_agent_view_dispatches_to_agents_machine(db_conn: psycopg.Connection) -> None:
    agent_id = _insert_agent(db_conn, "runner-a")
    view = completed(
        {
            "commands": [{"name": "project", "description": "d", "instruction_hint": "h"}],
            "mcp_names": ["runner-only-groundwork"],
        }
    )

    with (
        runner_ops(
            db_conn, "runner-a", {"agent_skill_view": expecting({"agent_id": agent_id}, view)}
        ),
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/commands?agent_id={agent_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "project", "description": "d", "instruction_hint": "h"}]


@pytest.mark.parametrize(
    "answer",
    [refused(404, "runner down"), failed({"error": "unknown op"})],
    ids=["unreachable", "failed"],
)
def test_endpoint_agent_view_unavailable_falls_back_locally(
    db_conn: psycopg.Connection, local_command: dict[str, str], answer: OpsReply
) -> None:
    agent_id = _insert_agent(db_conn, "runner-a")

    with (
        runner_ops(db_conn, "runner-a", {"agent_skill_view": answer}),
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/commands?agent_id={agent_id}")
    assert resp.status_code == 200, resp.text
    assert local_command in resp.json()


def test_endpoint_agent_view_missing_agent_falls_back_locally(
    local_command: dict[str, str],
) -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/commands?agent_id=999")
    assert resp.status_code == 200, resp.text
    assert local_command in resp.json()


def test_endpoint_agent_view_excludes_compact_for_external_harness(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _insert_agent(db_conn, "runner-a", harness="claude_code")
    view = completed(
        {
            "commands": [
                {"name": "compact", "description": "c", "instruction_hint": ""},
                {"name": "project", "description": "d", "instruction_hint": "h"},
            ],
            "mcp_names": [],
        }
    )

    with (
        runner_ops(
            db_conn, "runner-a", {"agent_skill_view": expecting({"agent_id": agent_id}, view)}
        ),
        gateway_test_client() as client,
    ):
        resp = client.get(f"/api/commands?agent_id={agent_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [{"name": "project", "description": "d", "instruction_hint": "h"}]
