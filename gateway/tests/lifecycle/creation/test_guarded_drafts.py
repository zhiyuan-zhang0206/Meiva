"""Guarded raw draft identity freezes the first birth despite later mutable state."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.http.auth.request_principal import AuthPrincipal, principal_key
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.lifecycle.creation.test_guarded_creation import HEADERS, SECRET
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
from ops.rpc_schemas import LaunchAgentRequest

CASES = [
    ("guide", {"nl": "one intent"}),
    ("schedules", {"nl": "one intent"}),
    ("packages", {"nl": "one intent", "kind": "skill"}),
]


@pytest.mark.parametrize("surface,body", CASES)
def test_raw_intent_replay_survives_defaults_and_prompt_changes(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    body: dict[str, str],
) -> None:
    path = f"/api/keyed/v1/{surface}/draft"
    first = client.post(path, json=body, headers=HEADERS)
    assert first.status_code == 200, first.text
    agent_id = first.json()["agent_id"]
    stored = db_conn.execute(
        "SELECT launch_attempt_id, prompt_inbound_id, prompt_content FROM agent_creation_snapshots"
    ).fetchone()
    assert stored is not None
    client.app.state.machine.set(name="no-longer-registered")
    replay = client.post(path, json=body, headers=HEADERS)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert db_conn.execute(
        "SELECT last_launch_attempt_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (stored[0],)
    assert db_conn.execute(
        "SELECT id, content FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchall() == [(stored[1], stored[2])]
    assert client.post(path, json={**body, "nl": "changed"}, headers=HEADERS).status_code == 409
    if surface == "packages":
        assert (
            client.post(path, json={**body, "kind": "plugin"}, headers=HEADERS).status_code == 409
        )


@pytest.mark.parametrize("mutation", ["rotated", "admitted", "terminated", "deleted"])
def test_historical_snapshot_never_wakes_later_work(
    client: TestClient, runner: RunnerOps, db_conn: psycopg.Connection, mutation: str
) -> None:
    path = "/api/keyed/v1/guide/draft"
    first = client.post(path, json={"nl": "one intent"}, headers=HEADERS)
    agent_id = first.json()["agent_id"]
    if mutation == "rotated":
        db_conn.execute(
            "UPDATE agents_meta SET last_launch_attempt_id=%s WHERE id=%s", (uuid4(), agent_id)
        )
    elif mutation == "admitted":
        db_conn.execute(
            "UPDATE agents_meta SET last_admission_at=now() WHERE id=%s",
            (agent_id,),
        )
    elif mutation == "terminated":
        db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    else:
        db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent_id,))
    db_conn.commit()

    launches_before = len(runner.calls_of("spawn-launch"))
    replay = client.post(path, json={"nl": "one intent"}, headers=HEADERS)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert len(runner.calls_of("spawn-launch")) == launches_before
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)


def test_concurrent_same_intent_commits_one_prompt(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    def submit(_index: int):
        return client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)

    with ThreadPoolExecutor(max_workers=3) as executor:
        responses = list(executor.map(submit, range(3)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["agent_id"] for response in responses}) == 1
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)
    fresh = client.post(
        "/api/keyed/v1/guide/draft",
        json={"nl": "one"},
        headers={**HEADERS, "Idempotency-Key": "new-intent"},
    )
    assert fresh.json()["agent_id"] != responses[0].json()["agent_id"]


def test_snapshot_failure_rolls_back_birth_and_prompt(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    with (
        failing_insert(db_conn, "agent_creation_snapshots", message="injected snapshot failure"),
        pytest.raises(psycopg.errors.RaiseException, match="injected snapshot"),
    ):
        client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)
    for table in ("agents", "inbound_messages", "agent_creation_snapshots"):
        assert db_conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608


@pytest.mark.parametrize("surface,body", CASES)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": ""},
        {**HEADERS, "Idempotency-Scope": "unknown"},
        {**HEADERS, "Idempotency-Key": "x" * 129},
    ],
)
def test_invalid_admission_has_zero_effect(
    client: TestClient,
    db_conn: psycopg.Connection,
    surface: str,
    body: dict[str, str],
    headers: dict[str, str],
) -> None:
    assert (
        client.post(f"/api/keyed/v1/{surface}/draft", json=body, headers=headers).status_code == 422
    )
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)


def test_lost_launch_ack_keeps_first_attempt_and_first_prompt(
    client: TestClient, db_conn: psycopg.Connection
) -> None:
    attempts: list[OpsCall] = []

    def lose_the_first_acknowledgement(call: OpsCall) -> OpsReply:
        # meiva:note
        # The runner woke the agent; only its acknowledgement never arrives whole.
        attempts.append(call)
        return OpsReply(200, "{") if len(attempts) == 1 else launched(call)

    with runner_ops(db_conn, "local-test", {"spawn-launch": lose_the_first_acknowledgement}):
        first = client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)
        assert first.status_code == 502, first.text
        retry = client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)
    assert retry.status_code == 200, retry.text
    assert len(attempts) == 2
    assert attempts[0].payload["launch_attempt_id"] == attempts[1].payload["launch_attempt_id"]
    assert attempts[0].payload["agent_id"] == attempts[1].payload["agent_id"]
    assert attempts[1].payload["agent_id"] == retry.json()["agent_id"]
    assert all("prompt" not in call.payload for call in attempts)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_replay_reauthenticates(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
) -> None:
    path = "/api/keyed/v1/guide/draft"
    assert client.post(path, json={"nl": "one"}, headers=HEADERS).status_code == 200
    with gateway_test_client(
        process=client.app.state.gateway_process,
        config=replace(
            client.app.state.gateway_config,
            cluster_secret="rotated-draft-secret",  # noqa: S106 -- isolated test credential
        ),
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as rotated:
        assert rotated.post(path, json={"nl": "one"}, headers=HEADERS).status_code == 401
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)


def test_deliberate_retry_rotates_pointer_but_draft_replays_original_attempt(
    client: GatewayTestClient,
    runner: RunnerOps,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from base.agents.birth.creation_identity import find_creation
    from ops.lifecycle.launch import validate_launch_row

    first = client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)
    agent_id = first.json()["agent_id"]
    row = db_conn.execute(
        "SELECT creation_key, request_hash, launch_attempt_id FROM agent_creation_snapshots"
    ).fetchone()
    assert row is not None
    key, digest, attempt = row

    retry = client.post(
        f"/api/keyed/v1/agents/{agent_id}/retry-launch",
        json={"expected_prior_attempt_id": str(attempt)},
        headers={**HEADERS, "Idempotency-Key": "deliberate-retry"},
    )
    assert retry.status_code == 200, retry.text
    assert len(runner.calls_of("launch-reconcile")) == 1
    replacement = retry.json()["launch_attempt_id"]
    assert replacement != str(attempt)
    original = find_creation(db_conn, key, digest)
    assert original is not None and original.launch_attempt_id == attempt
    assert not original.launch_pending
    with pytest.raises(ValueError, match="launch"):
        validate_launch_row(
            client.app.state.db_pool,
            LaunchAgentRequest(agent_id=agent_id, launch_attempt_id=attempt),
            local_machine=machine_identity.name(),
        )

    launches_before = len(runner.calls_of("spawn-launch"))
    replay = client.post("/api/keyed/v1/guide/draft", json={"nl": "one"}, headers=HEADERS)
    assert replay.status_code == 200 and replay.json() == first.json()
    # meiva:note
    # The original draft replay cannot launch the replacement attempt.
    assert len(runner.calls_of("spawn-launch")) == launches_before
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)


def test_concurrent_changed_raw_intents_conflict_in_birth_transaction(
    client: TestClient, db_conn: psycopg.Connection, db_url: str
) -> None:
    path = "/api/keyed/v1/guide/draft"
    key = principal_key(AuthPrincipal("cluster", "administrator"), "POST", path, "intent")
    # meiva:note
    # Both requests pass the receipt lookup, then queue on the key's birth lock; released
    # together, the database decides which birth is the winner.
    with ThreadPoolExecutor(max_workers=2) as executor:
        with held_creation_lock(db_url, key) as held:
            futures = [
                executor.submit(client.post, path, json={"nl": text}, headers=HEADERS)
                for text in ("one", "different")
            ]
            held.wait_for_waiters(2)
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM agent_creation_snapshots").fetchone() == (1,)
