from concurrent.futures import ThreadPoolExecutor
from typing import LiteralString
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from base.agents.birth.creation_identity import creation_request_hash, find_creation
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.http.auth.request_principal import AuthPrincipal, principal_key
from gateway.tests.auth.gateway_config import GatewayTestClient
from gateway.tests.lifecycle.creation.test_guarded_creation import HEADERS, PATH
from gateway.tests.lifecycle.creation.test_guarded_creation import client as client
from gateway.tests.lifecycle.creation.test_guarded_creation import runner as runner
from gateway.tests.support.db_faults.held_lock import held_creation_lock
from gateway.tests.support.db_faults.insert_fault import failing_insert
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    RunnerOps,
    launched,
    runner_ops,
)
from ops.rpc_schemas import LaunchAgentRequest, SpawnAgentRequest

BODY = {"machine": "local-test", "prompt": "one goal", "prompt_source": "user"}


def _snapshot(conn: psycopg.Connection) -> tuple[str, str, UUID]:
    row = conn.execute(
        "SELECT creation_key, request_hash, launch_attempt_id FROM agent_creation_snapshots"
    ).fetchone()
    assert row is not None
    return row


# meiva:note
# The target is no longer a runner: a fresh spawn would be refused at preflight.
def _registry_refuses_new_spawns(conn: psycopg.Connection) -> None:
    conn.execute("UPDATE machines SET role=ARRAY['gateway'] WHERE name='local-test'")
    conn.commit()


@pytest.mark.parametrize("mutation", ["admitted", "terminated", "deleted", "placed"])
def test_historical_birth_never_wakes_later_work(
    client: GatewayTestClient,
    runner: RunnerOps,
    db_conn: psycopg.Connection,
    mutation: str,
) -> None:
    first = client.post(PATH, json=BODY, headers=HEADERS)
    assert first.status_code == 201, first.text
    agent_id = first.json()["id"]
    statements: dict[str, LiteralString] = {
        "admitted": "UPDATE agents_meta SET last_admission_at=now() WHERE id=%s",
        "terminated": "UPDATE agents_meta SET status='terminated' WHERE id=%s",
        "deleted": "DELETE FROM agents_meta WHERE id=%s",
        "placed": "UPDATE agents_meta SET machine='later-placement' WHERE id=%s",
    }
    db_conn.execute(sql.SQL(statements[mutation]), (agent_id,))
    db_conn.commit()
    _registry_refuses_new_spawns(db_conn)
    launches_before = len(runner.calls_of("spawn-launch"))
    replay = client.post(PATH, json=BODY, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == agent_id
    assert replay.json()["accepted"] is True
    assert replay.json()["execution_observed"] is False
    assert len(runner.calls_of("spawn-launch")) == launches_before
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_original_manifest_precedes_mutable_config_and_defaults(
    client: GatewayTestClient, runner: RunnerOps, db_conn: psycopg.Connection
) -> None:
    body = {"prompt": "one goal", "prompt_source": "user"}
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    agent_id = first.json()["id"]
    stored = db_conn.execute(
        "SELECT machine, config_overlay, birth_config, launch_attempt_id "
        "FROM agent_creation_snapshots"
    ).fetchone()
    assert stored is not None
    changed = {"inject_skills": ["operator-added"]}
    db_conn.execute(
        "UPDATE agents_meta SET config_overlay=%s WHERE id=%s", (Jsonb(changed), agent_id)
    )
    db_conn.commit()
    client.app.state.machine.set(name="new-default")
    _registry_refuses_new_spawns(db_conn)
    first_launches = len(runner.calls_of("spawn-launch"))
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    launches = runner.payloads("spawn-launch")
    assert len(launches) == first_launches + 1
    launch = launches[-1]
    assert (
        runner.name,
        launch.get("config"),
        launch["birth_config"],
        UUID(launch["launch_attempt_id"]),
    ) == stored
    assert db_conn.execute(
        "SELECT config_overlay FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (changed,)
    key, digest, _ = _snapshot(db_conn)
    expected = creation_request_hash(SpawnAgentRequest.model_validate(body).model_dump(mode="json"))
    assert digest == expected
    assert key == principal_key(AuthPrincipal("cluster", "administrator"), "POST", PATH, "intent")


def test_original_key_cannot_dispatch_deliberate_replacement_attempt(
    client: GatewayTestClient,
    runner: RunnerOps,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    from ops.lifecycle.launch import validate_launch_row

    first = client.post(PATH, json=BODY, headers=HEADERS)
    assert first.status_code == 201, first.text
    agent_id = first.json()["id"]
    key, digest, original = _snapshot(db_conn)

    retry = client.post(
        f"/api/keyed/v1/agents/{agent_id}/retry-launch",
        json={"expected_prior_attempt_id": str(original)},
        headers={**HEADERS, "Idempotency-Key": "deliberate-retry"},
    )
    assert retry.status_code == 200, retry.text
    assert len(runner.calls_of("launch-reconcile")) == 1
    replacement = UUID(retry.json()["launch_attempt_id"])
    assert replacement != original
    validate_launch_row(
        client.app.state.db_pool,
        LaunchAgentRequest(agent_id=agent_id, launch_attempt_id=replacement),
        local_machine=machine_identity.name(),
    )
    with pytest.raises(ValueError, match="stale"):
        validate_launch_row(
            client.app.state.db_pool,
            LaunchAgentRequest(agent_id=agent_id, launch_attempt_id=original),
            local_machine=machine_identity.name(),
        )
    receipt = find_creation(db_conn, key, digest)
    assert (
        receipt is not None and receipt.launch_attempt_id == original and not receipt.launch_pending
    )

    launches_before = len(runner.calls_of("spawn-launch"))
    replay = client.post(PATH, json=BODY, headers=HEADERS)
    assert replay.status_code == 201 and replay.json()["id"] == agent_id
    # meiva:note
    # The old creation cannot dispatch a later retry attempt.
    assert len(runner.calls_of("spawn-launch")) == launches_before
    assert db_conn.execute("SELECT last_launch_attempt_id FROM agents_meta").fetchone() == (
        replacement,
    )
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_snapshot_fault_rolls_back_then_same_key_accepts_fresh_birth(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    with (
        failing_insert(db_conn, "agent_creation_snapshots", message="injected snapshot failure"),
        pytest.raises(psycopg.errors.RaiseException, match="injected snapshot"),
    ):
        client.post(PATH, json=BODY, headers=HEADERS)
    for statement in (
        "SELECT count(*) FROM agents",
        "SELECT count(*) FROM inbound_messages",
        "SELECT count(*) FROM audit_events WHERE event_name='spawn'",
        "SELECT count(*) FROM agent_creation_snapshots",
    ):
        assert db_conn.execute(statement).fetchone() == (0,)
    accepted = client.post(PATH, json=BODY, headers=HEADERS)
    assert accepted.status_code == 201, accepted.text
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)


@pytest.mark.parametrize("different", [False, True])
def test_same_key_birth_transaction_freezes_concurrent_winner(
    client: GatewayTestClient, db_conn: psycopg.Connection, db_url: str, different: bool
) -> None:
    key = principal_key(AuthPrincipal("cluster", "administrator"), "POST", PATH, "intent")
    bodies = [BODY, {**BODY, "prompt": "another goal"} if different else BODY]
    # meiva:note
    # Both requests pass the receipt lookup, then queue on the key's birth lock; released
    # together, the database decides which birth is the winner.
    with ThreadPoolExecutor(max_workers=2) as executor:
        with held_creation_lock(db_url, key) as held:
            futures = [
                executor.submit(client.post, PATH, json=body, headers=HEADERS) for body in bodies
            ]
            held.wait_for_waiters(2)
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == (
        [201, 409] if different else [201, 201]
    )
    successful = next(response for response in responses if response.status_code == 201)
    row = db_conn.execute(
        "SELECT agent_id, request_hash, launch_attempt_id, prompt_content FROM agent_creation_snapshots"
    ).fetchone()
    assert row is not None and row[0] == successful.json()["id"]
    assert row[1] == creation_request_hash(
        SpawnAgentRequest.model_validate(bodies[responses.index(successful)]).model_dump(
            mode="json"
        )
    )
    assert db_conn.execute("SELECT last_launch_attempt_id FROM agents_meta").fetchone() == (row[2],)
    assert db_conn.execute("SELECT content FROM inbound_messages").fetchone() == (row[3],)
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='spawn'"
    ).fetchone() == (1,)


def test_native_launch_ack_loss_recovers_same_attempt(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    attempts: list[OpsCall] = []

    def lose_the_first_acknowledgement(call: OpsCall) -> OpsReply:
        # meiva:note
        # The runner acted on the launch; only its acknowledgement never arrives whole.
        attempts.append(call)
        return OpsReply(200, "{") if len(attempts) == 1 else launched(call)

    with runner_ops(db_conn, "local-test", {"spawn-launch": lose_the_first_acknowledgement}):
        first = client.post(PATH, json=BODY, headers=HEADERS)
        assert first.status_code == 502, first.text
        snapshot = _snapshot(db_conn)
        retry = client.post(PATH, json=BODY, headers=HEADERS)
    assert retry.status_code == 201, retry.text
    assert len(attempts) == 2
    assert all(UUID(call.payload["launch_attempt_id"]) == snapshot[2] for call in attempts)
    assert all(call.payload["agent_id"] == retry.json()["id"] for call in attempts)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert _snapshot(db_conn) == snapshot
