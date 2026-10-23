# meiva:note
# POST /api/keyed/v1/packages/draft — the skill / plugin / MCP install entry.
#
# Spawns an meiva-package-installer agent for a natural-language "I want a
# capability like X" request. The spawn runs for real against the gateway's own in-process
# runner; the tests read the committed agent back and assert the wiring: the prompt points at
# `meiva.skills.meiva_guide.packages.install`, carries the user's request verbatim and which
# kind the entry point was for, labels the agent `meiva-package-installer`, and the spawned id
# comes back as agent_id. There is no URL/spec field by design — every install goes through
# the agent.

import psycopg
import pytest

from gateway.tests.auth.gateway_config import gateway_test_client, spawn_headers
from gateway.tests.extensions.draft_support import DraftedAgent, drafted_agent
from gateway.tests.support.runner_ops.ops_stub import RunnerOps

DRAFT_PATH = "/api/keyed/v1/packages/draft"


def _draft(db_conn: psycopg.Connection, kind: str, nl: str) -> tuple[int, DraftedAgent]:
    with gateway_test_client() as client:
        r = client.post(DRAFT_PATH, json={"kind": kind, "nl": nl}, headers=spawn_headers("intent"))
    assert r.status_code == 200, r.text
    agent_id = r.json()["agent_id"]
    return agent_id, drafted_agent(db_conn, agent_id)


@pytest.mark.parametrize(
    ("kind", "brief_marker"),
    [
        ("skill", "instruction pack"),
        ("plugin", "RUN CODE"),
        ("mcp", "RUNS AS A PROCESS"),
    ],
)
def test_draft_spawns_installer_for_each_kind(
    db_conn: psycopg.Connection,
    gateway_local_runner: RunnerOps,
    kind: str,
    brief_marker: str,
) -> None:
    """Each kind spawns the same installer, framed with that kind's trust cost —
    the framing is what the skill turns into the confirm gate."""
    agent_id, drafted = _draft(db_conn, kind, "something that reads my Obsidian vault")
    (launch,) = gateway_local_runner.calls_of("spawn-launch")
    assert launch.payload["agent_id"] == agent_id
    assert drafted.label == "meiva-package-installer"
    assert "meiva.skills.meiva_guide.packages.install" in drafted.prompt
    assert "something that reads my Obsidian vault" in drafted.prompt
    assert f"Kind: {kind}" in drafted.prompt
    assert brief_marker in drafted.prompt


def test_draft_prompt_orders_the_whole_lifecycle(db_conn: psycopg.Connection) -> None:
    """Installing is the middle of the job — the prompt must also order the
    verify / judge / report steps, not just 'install it'."""
    _agent_id, drafted = _draft(db_conn, "mcp", "linear issues")
    for phrase in ("confirm with the user", "spawn a test agent", "adaptation"):
        assert phrase in drafted.prompt, f"missing lifecycle step: {phrase}"


def test_draft_rejects_an_unknown_kind(
    db_conn: psycopg.Connection, gateway_local_runner: RunnerOps
) -> None:
    """`kind` is a closed enum — an unknown value is a 422, never a spawn."""
    with gateway_test_client() as client:
        r = client.post(
            DRAFT_PATH, json={"kind": "npm", "nl": "left-pad"}, headers=spawn_headers("intent")
        )
    assert r.status_code == 422, r.text
    assert gateway_local_runner.calls == []
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)
