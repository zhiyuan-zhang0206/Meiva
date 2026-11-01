# meiva:note
# A restart request is admitted against the model catalog at the gateway, before any forward.

import psycopg

from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.support.runner_ops.ops_stub import failed, runner_ops


def _agent_on(conn: psycopg.Connection, machine: str) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO agents_meta(id,spawner,status,machine) VALUES (%s,'user','idling',%s)",
        (row[0], machine),
    )
    conn.commit()
    return row[0]


def test_gateway_membership_refusal_is_422_before_forward(db_conn: psycopg.Connection) -> None:
    agent_id = _agent_on(db_conn, "remote-runner")
    with (
        runner_ops(db_conn, "remote-runner", {}),
        gateway_test_client() as client,
    ):
        refused = client.post(
            f"/api/agents/{agent_id}/restart",
            json={"message": "restarting", "config_overlay": {"llm_model": "unknown-model"}},
        )
    # meiva:note
    # an invalid overlay is refused here: the runner has no scripted `lifecycle` reply, so a
    # forward would have come back as a bad gateway rather than this 422
    assert refused.status_code == 422


def test_gateway_preserves_runner_typed_400(db_conn: psycopg.Connection) -> None:
    agent_id = _agent_on(db_conn, "remote-runner")
    refusal = failed(
        {
            "error": "InvalidModelConfig: provider configuration is invalid",
            "detail": "provider configuration is invalid",
            "reason": "invalid_model_config",
        }
    )
    with (
        runner_ops(db_conn, "remote-runner", {"lifecycle": refusal}),
        gateway_test_client() as client,
    ):
        response = client.post(f"/api/agents/{agent_id}/restart", json={"message": "restarting"})
    assert response.status_code == 400
    assert response.json()["reason"] == "invalid_model_config"
    assert response.json()["detail"] == "provider configuration is invalid"
