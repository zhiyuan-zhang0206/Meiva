# meiva:note
# POST /api/keyed/v1/guide/draft — the Meiva Guide entry.
#
# Spawns an meiva-guide agent for a natural-language ops request. The spawn runs for real
# against the gateway's own in-process runner; the test reads the committed agent back and
# asserts the wiring: the fixed prompt points at the ROOT `meiva.skills.meiva_guide` skill,
# carries the user's request, labels the agent `meiva-guide`, and the spawned id is returned
# as agent_id.

import psycopg
import pytest

from gateway.tests.auth.gateway_config import gateway_test_client, spawn_headers
from gateway.tests.extensions.draft_support import drafted_agent
from gateway.tests.support.runner_ops.local_runner import executed_ops


@pytest.mark.usefixtures("gateway_local_runner")
def test_draft_spawns_meiva_guide_agent(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        r = client.post(
            "/api/keyed/v1/guide/draft",
            json={"nl": "install the linear MCP server"},
            headers=spawn_headers("intent"),
        )
    assert r.status_code == 200, r.text
    agent_id = r.json()["agent_id"]
    launched = executed_ops(db_conn, "spawn-launch")
    assert [(op.status, op.result) for op in launched] == [("completed", {"id": agent_id})]
    drafted = drafted_agent(db_conn, agent_id)
    assert drafted.label == "meiva-guide"
    assert "install the linear MCP server" in drafted.prompt
    assert "meiva.skills.meiva_guide" in drafted.prompt
