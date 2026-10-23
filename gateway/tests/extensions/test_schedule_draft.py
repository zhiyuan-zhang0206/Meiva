"""POST /api/keyed/v1/schedules/draft — the schedule-writer entry."""

import psycopg

from gateway.tests.auth.gateway_config import gateway_test_client, spawn_headers
from gateway.tests.extensions.draft_support import drafted_agent
from gateway.tests.support.runner_ops.ops_stub import RunnerOps


def test_draft_spawns_writer_agent(
    db_conn: psycopg.Connection, gateway_local_runner: RunnerOps
) -> None:
    with gateway_test_client() as client:
        r = client.post(
            "/api/keyed/v1/schedules/draft",
            json={"nl": "consolidate memory nightly"},
            headers=spawn_headers("intent"),
        )
    assert r.status_code == 200, r.text
    agent_id = r.json()["agent_id"]
    (launch,) = gateway_local_runner.calls_of("spawn-launch")
    assert launch.payload["agent_id"] == agent_id
    drafted = drafted_agent(db_conn, agent_id)
    assert drafted.label == "meiva-schedule-writer"
    assert "consolidate memory nightly" in drafted.prompt
    assert "meiva.skills.meiva_guide.schedules" in drafted.prompt
