from concurrent.futures import ThreadPoolExecutor
from typing import Any, LiteralString

import psycopg
import pytest

from gateway.agents.task_assignment import router as assignments
from gateway.agents.task_assignment.schemas import TaskAssignmentIn
from gateway.http.auth.request_principal import AuthPrincipal, principal_key
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
)
from gateway.tests.lifecycle.test_task_assignments import HEADERS, PATH, SECRET
from gateway.tests.lifecycle.test_task_assignments import body as body
from gateway.tests.lifecycle.test_task_assignments import client as client
from gateway.tests.lifecycle.test_task_assignments import runner as runner
from gateway.tests.support.db_faults.held_lock import held_table_lock
from gateway.tests.support.db_faults.insert_fault import failing_insert
from gateway.tests.support.runner_ops.ops_stub import RunnerOps
from ops.rpc_schemas import SpawnAgentRequest


def _counts(conn: psycopg.Connection) -> tuple[int, ...]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM agents),(SELECT count(*) FROM agent_tasks),(SELECT count(*) FROM audit_events),(SELECT count(*) FROM inbound_messages),(SELECT count(*) FROM task_assignment_receipts)"
    ).fetchone()
    assert row is not None
    conn.commit()
    return row


def test_fresh_simultaneous_intents_produce_one_pair(
    client: GatewayTestClient, db_conn: psycopg.Connection, db_url: str, body: dict[str, Any]
) -> None:
    before = _counts(db_conn)
    # meiva:note
    # Three fresh intents each find no acceptance, then stop at the preflight's registry read;
    # released together they race to commit the pair.
    with ThreadPoolExecutor(max_workers=3) as executor:
        with held_table_lock(db_url, "machines") as held:
            futures = [
                executor.submit(client.post, PATH, json=body, headers=HEADERS) for _ in range(3)
            ]
            held.wait_for_waiters(3)
        responses = [future.result(timeout=20) for future in futures]
    assert all(response.status_code == 201 for response in responses), [r.text for r in responses]
    assert len({r.json()["agent_id"] for r in responses}) == 1
    assert len({r.json()["task"]["id"] for r in responses}) == 1
    assert len({r.json()["launch_attempt_id"] for r in responses}) == 1
    after = _counts(db_conn)
    assert after[0] == before[0] + 1
    assert after[1] == before[1] + 1
    assert after[3] == before[3] + 1
    assert after[4] == before[4] + 1


# meiva:note
# Each stage's last real write, which raises after its row is inserted.
_STAGE_TABLE = {
    "birth": "agent_creation_snapshots",
    "task": "agent_tasks",
    "notification": "inbound_messages",
    "receipt": "task_assignment_receipts",
}


@pytest.mark.parametrize("stage", list(_STAGE_TABLE))
def test_exception_after_each_real_writer_rolls_back_all_effects(
    client: GatewayTestClient, db_conn: psycopg.Connection, body: dict[str, Any], stage: str
) -> None:
    before = _counts(db_conn)
    with (
        failing_insert(db_conn, _STAGE_TABLE[stage], message=f"failure after {stage}"),
        pytest.raises(psycopg.errors.RaiseException, match=f"failure after {stage}"),
    ):
        client.post(PATH, json=body, headers=HEADERS)
    assert _counts(db_conn) == before


# meiva:note
# Another intent for the same key commits while this request's preflight waits on the registry.
#
# Returns this request's response and the winner's committed result.
def _winner_commits_while_preflight_waits(
    client: GatewayTestClient,
    db_url: str,
    body: dict[str, Any],
    *,
    registry_changes: LiteralString | None,
) -> tuple[Any, dict[str, Any]]:
    app = client.app
    key = principal_key(AuthPrincipal("cluster", "administrator"), "POST", PATH, "compound")
    raw = TaskAssignmentIn.model_validate(body).model_dump(mode="json")
    spawn = SpawnAgentRequest(
        spawner=f"agent:{body['actor_agent_id']}", machine="local-test", label=None, config=None
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        with held_table_lock(db_url, "machines") as held:
            pending = executor.submit(client.post, PATH, json=body, headers=HEADERS)
            held.wait_for_waiters(1)
            winner, _events = assignments.commit_assignment(
                app.state.db_pool,
                key,
                raw,
                TaskAssignmentIn.model_validate(body),
                spawn,
                "local-test",
                catalog=assignments.require_catalog(app.state.catalog),
                authority=app.state.config_authority,
            )
            if registry_changes is not None:
                held.holder.execute(registry_changes)
        replay = pending.result(timeout=20)
    return replay, winner.model_dump(mode="json")


def test_preflight_failure_returns_winner_committed_during_its_window(
    client: GatewayTestClient, db_url: str, body: dict[str, Any]
) -> None:
    replay, winner = _winner_commits_while_preflight_waits(
        client,
        db_url,
        body,
        # meiva:note
        # The registry moves on while the request waits: its preflight now fails.
        registry_changes="DELETE FROM machines WHERE name='local-test'",
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["task"] == winner["task"]
    assert replay.json()["agent_id"] == winner["agent_id"]


def test_second_lookup_replays_winner_after_successful_preflight_window(
    client: GatewayTestClient, db_url: str, body: dict[str, Any]
) -> None:
    replay, winner = _winner_commits_while_preflight_waits(
        client, db_url, body, registry_changes=None
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["task"] == winner["task"]
    assert replay.json()["agent_id"] == winner["agent_id"]


def test_single_connection_pool_accepts_without_nested_borrow(
    runner: RunnerOps, body: dict[str, Any]
) -> None:
    with gateway_test_client(
        config=gateway_test_config(db_pool_max_size=1, cluster_secret=SECRET),
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as client:
        response = client.post(PATH, json=body, headers=HEADERS)
        assert response.status_code == 201, response.text
        again = client.post(PATH, json=body, headers=HEADERS)
        assert again.status_code == 201
        assert again.json()["task"] == response.json()["task"]


def test_title_rejection_rolls_back_a_different_key_birth(
    client: GatewayTestClient, body: dict[str, Any], db_conn: psycopg.Connection
) -> None:
    assert client.post(PATH, json=body, headers=HEADERS).status_code == 201
    before = _counts(db_conn)
    rejected = client.post(
        PATH, json=body, headers={**HEADERS, "Idempotency-Key": "another-operation"}
    )
    assert rejected.status_code == 422, rejected.text
    assert _counts(db_conn) == before


def test_closed_parent_rejection_does_not_leave_agent_or_receipt(
    client: GatewayTestClient, body: dict[str, Any], db_conn: psycopg.Connection
) -> None:
    row = db_conn.execute(
        "INSERT INTO agent_tasks(title,description,status,created_by) VALUES ('Closed parent','','done','system') RETURNING id"
    ).fetchone()
    assert row is not None
    db_conn.commit()
    body["task"]["parent"] = row[0]
    before = _counts(db_conn)
    rejected = client.post(PATH, json=body, headers=HEADERS)
    assert rejected.status_code == 422, rejected.text
    assert _counts(db_conn) == before
