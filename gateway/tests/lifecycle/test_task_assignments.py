# meiva:link gateway/docs/error-responses-and-middleware-order.meiva.okf.md
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.http.model_catalog import CatalogLoadFailure
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.support.db_faults.insert_fault import failing_insert
from gateway.tests.support.db_faults.missing_table import missing_table
from gateway.tests.support.runner_ops.ops_stub import RunnerOps, launched, refused, runner_ops

PATH = "/api/keyed/v1/task-assignments"
HEADERS = {"Idempotency-Key": "compound", "Idempotency-Scope": "principal-v1"}
SECRET = "compound-test-secret"  # noqa: S105


@pytest.fixture
def runner(
    gateway_data_plane: None, db_conn: psycopg.Connection, set_machine_identity: Any
) -> Generator[RunnerOps]:
    """The runner `local-test`, which accepts every launch it is asked for."""
    set_machine_identity(role="agent-runner", name="local-test")
    with runner_ops(db_conn, "local-test", {"spawn-launch": launched}) as value:
        yield value


@pytest.fixture
def client(
    runner: RunnerOps, *, machine_identity: MachineIdentityReader
) -> Generator[GatewayTestClient]:
    with gateway_test_client(
        cluster_secret=SECRET,
        headers={"Authorization": f"Bearer {SECRET}"},
        machine=machine_identity,
    ) as value:
        yield value


@pytest.fixture
def body(runner: RunnerOps, db_conn: psycopg.Connection) -> dict[str, Any]:
    actor = db_conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert actor is not None
    db_conn.execute(
        "INSERT INTO agents_meta(id,spawner,status,machine) VALUES (%s,'user','idling','local-test')",
        (actor[0],),
    )
    parent = db_conn.execute(
        "INSERT INTO agent_tasks(title,description,status,created_by,is_root) VALUES ('Root','','in_progress','system',true) RETURNING id"
    ).fetchone()
    assert parent is not None
    db_conn.commit()
    return {
        "actor_agent_id": actor[0],
        "task": {"title": "One assignment", "description": "Work", "parent": parent[0]},
        "agent": {"machine": "local-test"},
    }


def _counts(conn: psycopg.Connection) -> tuple[int, ...]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM agents),(SELECT count(*) FROM agent_tasks),(SELECT count(*) FROM audit_events),(SELECT count(*) FROM inbound_messages),(SELECT count(*) FROM task_assignment_receipts)"
    ).fetchone()
    assert row is not None
    conn.commit()
    return row


def test_response_loss_and_concurrent_duplicates_return_original_pair(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    before = _counts(db_conn)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(client.post, PATH, json=body, headers=HEADERS) for _ in range(3)]
        responses = [future.result() for future in futures]
    for response in responses:
        assert response.status_code == 201, response.text
        for field in ("task", "agent_id", "launch_attempt_id"):
            assert response.json()[field] == first.json()[field]
    assert _counts(db_conn) == before
    changed = {**body, "task": {**body["task"], "description": "Different"}}
    assert client.post(PATH, json=changed, headers=HEADERS).status_code == 409


def test_model_failure_preserves_accepted_pair_and_blocks_new_assignment(
    client: GatewayTestClient, db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    before = _counts(db_conn)
    client.app.state.catalog = CatalogLoadFailure(RuntimeError("provider failed after restart"))

    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    for field in ("task", "agent_id", "launch_attempt_id"):
        assert replay.json()[field] == first.json()[field]
    assert "ModelCatalogUnavailableError" in replay.json()["launch_failure"]
    assert _counts(db_conn) == before

    blocked = client.post(PATH, json=body, headers={**HEADERS, "Idempotency-Key": "new-assignment"})
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()["code"] == "model_catalog_unavailable"
    assert _counts(db_conn) == before


def test_producer_failure_rolls_back_entire_pair(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    before = _counts(db_conn)
    # meiva:note
    # The receipt is the last write, after birth, task, audit and inbound have been written.
    with (
        failing_insert(
            db_conn, "task_assignment_receipts", message="receipt failure after birth/task"
        ),
        pytest.raises(psycopg.errors.RaiseException, match="receipt failure"),
    ):
        client.post(PATH, json=body, headers=HEADERS)
    assert _counts(db_conn) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_agent_id": True},
        {"actor_agent_id": 0},
        {"unknown": 1},
        {"agent": {"fork_from": 1}},
        {"task": {"title": "X", "description": "", "parent": True}},
        {"task": {"title": "X", "description": "", "parent": 1, "priority": "unknown"}},
    ],
)
def test_invalid_raw_input_cannot_create_pair(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, Any], changes: dict[str, Any]
) -> None:
    before = _counts(db_conn)
    assert client.post(PATH, json={**body, **changes}, headers=HEADERS).status_code == 422
    assert _counts(db_conn) == before


def test_snapshot_replays_after_task_and_birth_metadata_deleted(
    client: TestClient,
    db_conn: psycopg.Connection,
    body: dict[str, Any],
) -> None:
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    accepted = first.json()
    db_conn.execute("DELETE FROM agent_tasks WHERE id=%s", (accepted["task"]["id"],))
    db_conn.execute("DELETE FROM inbound_messages WHERE agent_id=%s", (accepted["agent_id"],))
    db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (accepted["agent_id"],))
    db_conn.execute("DELETE FROM machines WHERE name='local-test'")
    db_conn.commit()
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    assert replay.json()["task"] == accepted["task"]
    assert replay.json()["agent_id"] == accepted["agent_id"]
    assert replay.json()["launch"]["accepted"] is True
    assert replay.json()["launch"]["execution_observed"] is False


def test_replay_skips_changed_task_state_and_defaults(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    body["agent"]["config"] = {"reasoning_effort": "high"}
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    original = first.json()
    db_conn.execute(
        "UPDATE agent_tasks SET title='Renamed',status='done' WHERE id=%s",
        (original["task"]["id"],),
    )
    db_conn.commit()
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201, replay.text
    assert replay.json()["task"] == original["task"]
    explicit = {
        **body,
        "task": {
            **body["task"],
            "remind_interval_seconds": original["task"]["remind_interval_seconds"],
        },
    }
    assert client.post(PATH, json=explicit, headers=HEADERS).status_code == 409


def test_launch_failure_retains_accepted_pair_and_original_attempt(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    with runner_ops(
        db_conn, "local-test", {"spawn-launch": refused(404, "runner unavailable")}
    ) as unavailable:
        first = client.post(PATH, json=body, headers=HEADERS)
        assert first.status_code == 201, first.text
        original = first.json()
        assert original["launch"] is None and original["launch_failure"]
        assert original["retry_launch_path"].endswith("/retry-launch")
        before = _counts(db_conn)
        again = client.post(PATH, json=body, headers=HEADERS)
    assert again.status_code == 201
    assert again.json()["task"] == original["task"]
    assert again.json()["launch_attempt_id"] == original["launch_attempt_id"]
    attempts = [payload["launch_attempt_id"] for payload in unavailable.payloads("spawn-launch")]
    assert len(attempts) == 2 and attempts[0] == attempts[1] == original["launch_attempt_id"]
    assert _counts(db_conn) == before


def test_changed_placement_returns_historical_acceptance_without_launch(
    client: TestClient, runner: RunnerOps, db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    original = first.json()
    history = db_conn.execute("SELECT birth_snapshot FROM task_assignment_receipts").fetchone()
    assert history is not None
    assert history[0]["preset_name"] is None
    db_conn.execute(
        "UPDATE agents_meta SET machine='operator-new-machine',config_overlay='{}' WHERE id=%s",
        (original["agent_id"],),
    )
    db_conn.commit()
    launches_before = len(runner.calls_of("spawn-launch"))
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201
    assert len(runner.calls_of("spawn-launch")) == launches_before
    assert replay.json()["launch_attempt_id"] == original["launch_attempt_id"]
    assert replay.json()["launch"]["accepted"] is True
    assert (
        db_conn.execute("SELECT birth_snapshot FROM task_assignment_receipts").fetchone() == history
    )
    assert history[0]["machine"] == "local-test"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": "x"},
        {"Idempotency-Key": "", "Idempotency-Scope": "principal-v1"},
        {"Idempotency-Key": "x", "Idempotency-Scope": "legacy"},
    ],
)
def test_missing_or_wrong_admission_headers_cannot_write(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, Any], headers: dict[str, str]
) -> None:
    before = _counts(db_conn)
    assert client.post(PATH, json=body, headers=headers).status_code == 422
    assert _counts(db_conn) == before


# meiva:link gateway/docs/error-responses-and-middleware-order.meiva.okf.md
def test_revoked_principals_cannot_replay(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    body: dict[str, Any],
) -> None:
    assert client.post(PATH, json=body, headers=HEADERS).status_code == 201
    before = _counts(db_conn)
    with gateway_test_client(
        process=client.app.state.gateway_process,
        config=replace(
            client.app.state.gateway_config,
            cluster_secret="rotated-compound-secret",  # noqa: S106
        ),
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as rotated:
        assert rotated.post(PATH, json=body, headers=HEADERS).status_code == 401
    assert _counts(db_conn) == before


def test_cookie_and_bearer_share_verified_subject(client: TestClient, body: dict[str, Any]) -> None:
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201
    assert client.post("/api/auth/login", json={"password": SECRET}).status_code == 200
    client.headers.pop("Authorization")
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201
    assert replay.json()["task"] == first.json()["task"]


def test_postcommit_launch_observation_failure_preserves_pair(
    client: TestClient, body: dict[str, Any], db_conn: psycopg.Connection
) -> None:
    first = client.post(PATH, json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    # meiva:note
    # The accepted pair is durable; reading the creation snapshot of its launch now fails.
    with missing_table(db_conn, "agent_creation_snapshots"):
        unobserved = client.post(PATH, json=body, headers=HEADERS)
    assert unobserved.status_code == 201, unobserved.text
    assert unobserved.json()["launch"] is None
    assert "UndefinedTable" in unobserved.json()["launch_failure"]
    assert unobserved.json()["task"] == first.json()["task"]
    replay = client.post(PATH, json=body, headers=HEADERS)
    assert replay.status_code == 201
    assert replay.json()["task"] == first.json()["task"]
    assert replay.json()["launch_attempt_id"] == first.json()["launch_attempt_id"]


def test_corrupt_snapshot_fails_without_recreating_mutable_state(
    client: TestClient, body: dict[str, Any], db_conn: psycopg.Connection
) -> None:
    assert client.post(PATH, json=body, headers=HEADERS).status_code == 201
    before = _counts(db_conn)
    db_conn.execute(
        "UPDATE task_assignment_receipts SET result=jsonb_set(result,'{task}',(result->'task')-'priority')"
    )
    db_conn.commit()
    with pytest.raises(ValueError, match="missing or unknown"):
        client.post(PATH, json=body, headers=HEADERS)
    assert _counts(db_conn) == before
