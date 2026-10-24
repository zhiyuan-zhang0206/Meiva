"""Committed launch failure and same-identity recovery at the HTTP boundary."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.tests.auth.gateway_config import gateway_test_client, spawn_via_http
from gateway.tests.support.db_faults.insert_fault import failing_insert
from gateway.tests.support.db_faults.missing_table import vanishing_table
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    RunnerOps,
    refused,
    runner_ops,
)
from gateway.tests.test_agents_endpoints import inbound_rows


def _assert_failed_birth_visible(client: TestClient, body: dict[str, Any], agent_id: int) -> None:
    assert body["retryable"] is False
    assert body["state"]["status"] == "idling"
    assert body["state"]["availability"]["reason"] == "launch_unreachable"
    assert body["retry_launch_path"] == f"/api/keyed/v1/agents/{agent_id}/retry-launch"
    assert (
        client.get(f"/api/agents/{agent_id}").json()["availability"]["reason"]
        == "launch_unreachable"
    )
    roster = client.get("/api/agents/roster").json()["agents"]
    assert (
        next(row for row in roster if row["agent_id"] == agent_id)["availability"]["reason"]
        == "launch_unreachable"
    )


@pytest.fixture
def offline_runner(
    db_conn: psycopg.Connection, machine_identity: MachineIdentity
) -> Generator[RunnerOps]:
    """The gateway's own machine, whose ops server does not answer a launch."""
    with runner_ops(
        db_conn,
        machine_identity.name(),
        {"spawn-launch": refused(404, "runner offline")},
        roles=("gateway", "agent-runner"),
    ) as value:
        yield value


def test_failed_plain_launch_persists_prompt_and_advertises_guarded_recovery(
    offline_runner: RunnerOps, db_conn: psycopg.Connection
) -> None:
    with gateway_test_client() as client:
        failed = spawn_via_http(client, {"prompt": "Do the task", "prompt_source": "user"})
        assert failed.status_code == 502
        body = failed.json()
        agent_id = body["agent_id"]
        _assert_failed_birth_visible(client, body, agent_id)
        assert inbound_rows(db_conn, agent_id) == [("Do the task", "chat", "user")]
    assert [call.payload["agent_id"] for call in offline_runner.calls_of("spawn-launch")] == [
        agent_id
    ]


def test_failed_launch_state_write_outage_keeps_committed_id_retriable(
    db_conn: psycopg.Connection, db_url: str, machine_identity: MachineIdentity
) -> None:
    with vanishing_table(db_url, "agents_meta") as store:

        def lose_the_store_then_fail(_call: OpsCall) -> OpsReply:
            # meiva:note
            # Birth has committed; only the ensuing launch-state transaction can fail.
            store.vanish()
            return refused(404, "runner offline")

        with (
            runner_ops(
                db_conn,
                machine_identity.name(),
                {"spawn-launch": lose_the_store_then_fail},
                roles=("gateway", "agent-runner"),
            ),
            gateway_test_client() as client,
        ):
            failed = spawn_via_http(client, {"prompt": "Keep me", "prompt_source": "user"})
        store.restore()
    assert failed.status_code == 502
    body = failed.json()
    assert body["agent_id"] > 0
    assert body["state"] == {"status": "unknown", "availability": None}
    assert body["retry_launch_path"] == f"/api/keyed/v1/agents/{body['agent_id']}/retry-launch"
    assert inbound_rows(db_conn, body["agent_id"]) == [("Keep me", "chat", "user")]


@pytest.mark.usefixtures("gateway_local_runner")
def test_failed_fork_launch_keeps_marker_and_prompt_in_one_birth(
    db_conn: psycopg.Connection, machine_identity: MachineIdentity
) -> None:
    with gateway_test_client() as client:
        # meiva:note
        # The source is launched by the gateway's own runner; only the fork's launch fails.
        source = spawn_via_http(client, {}).json()["id"]
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_id, parent_checkpoint_id, "
                "checkpoint, metadata) VALUES (%s, 'fork-base', NULL, '{}'::jsonb, '{}'::jsonb)",
                (str(source),),
            )
        db_conn.commit()
        with runner_ops(
            db_conn,
            machine_identity.name(),
            {"spawn-launch": refused(404, "runner offline")},
            roles=("gateway", "agent-runner"),
        ):
            failed = spawn_via_http(
                client, {"fork_from": source, "prompt": "Continue here", "prompt_source": "user"}
            )
    assert failed.status_code == 502
    agent_id = failed.json()["agent_id"]
    assert inbound_rows(db_conn, agent_id) == [
        ("", "fork", f"agent:{source}"),
        ("Continue here", "chat", "user"),
    ]


def test_admission_winning_dispatch_failure_returns_accepted_receipt(
    db_conn: psycopg.Connection, db_url: str, machine_identity: MachineIdentity
) -> None:
    def admit_then_fail(call: OpsCall) -> OpsReply:
        # meiva:note
        # The agent host admitted the agent, then the response was lost.
        with psycopg.connect(db_url, autocommit=True) as admitting:
            admitting.execute(
                "UPDATE agents_meta SET last_admission_at=clock_timestamp() WHERE id=%s",
                (call.payload["agent_id"],),
            )
        return refused(404, "response lost")

    with (
        runner_ops(
            db_conn,
            machine_identity.name(),
            {"spawn-launch": admit_then_fail},
            roles=("gateway", "agent-runner"),
        ),
        gateway_test_client() as client,
    ):
        response = spawn_via_http(client, {"prompt": "Stay", "prompt_source": "user"})
    assert response.status_code == 201
    agent_id = response.json()["id"]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT last_launch_failure_reason FROM agents_meta WHERE id=%s",
            (agent_id,),
        )
        assert cur.fetchone() == (None,)
    assert inbound_rows(db_conn, agent_id) == [("Stay", "chat", "user")]


@pytest.mark.usefixtures("gateway_local_runner")
def test_first_prompt_insert_failure_rolls_back_agent_row(db_conn: psycopg.Connection) -> None:
    before = db_conn.execute("SELECT count(*) FROM agents_meta").fetchone()
    with (
        failing_insert(db_conn, "inbound_messages", message="prompt insert refused"),
        gateway_test_client() as client,
        pytest.raises(psycopg.errors.RaiseException, match="prompt insert refused"),
    ):
        spawn_via_http(client, {"prompt": "Work", "prompt_source": "user"})
    assert db_conn.execute("SELECT count(*) FROM agents_meta").fetchone() == before
