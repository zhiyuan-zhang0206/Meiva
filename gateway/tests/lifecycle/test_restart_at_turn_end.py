# meiva:note
# `POST /api/agents/{id}/restart` with `at_turn_end`, the agent's own restart: it passes the same
# validation as any restart (a refusal answers the caller at once, nothing is recorded) and then
# the home runner records it as a pending command, which a running agent may ask for although a
# restart sent by anyone else refuses it with 409 (`test_cancel_compact_routes.py`).
# meiva:link gateway/agents/lifecycle.py

from __future__ import annotations

import psycopg
import pytest

from base.api_contracts.agent_contract import Harness, HarnessBilling
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import MachineIdentitySetter
from gateway.schemas.models import HarnessAvailability, HarnessModelOption
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client, spawn_via_http
from gateway.tests.support.runner_ops.ops_stub import RunnerOps


def _seed_codex(client: GatewayTestClient, machine: str) -> None:
    client.app.state.harness_cache.store(
        machine,
        [
            HarnessAvailability(
                harness=Harness.CODEX,
                status="available",
                reason="signed in",
                launchable=True,
                billing=HarnessBilling.SUBSCRIPTION,
                models=[
                    HarnessModelOption(
                        id="gpt-6.1-sol",
                        reasoning_effort_options=["low", "medium", "high"],
                        reasoning_effort_default="medium",
                    )
                ],
            ),
            HarnessAvailability(
                harness=Harness.ANTIGRAVITY,
                status="unavailable",
                reason="not signed in",
                launchable=False,
                billing=HarnessBilling.SUBSCRIPTION,
                models=[],
            ),
        ],
    )


@pytest.fixture
def _local_machine(
    set_machine_identity: MachineIdentitySetter, gateway_local_runner: RunnerOps
) -> MachineIdentityReader:
    return set_machine_identity(role="agent-runner")


def _set_status(db_conn: psycopg.Connection, agent_id: int, status: str) -> None:
    db_conn.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (status, agent_id))
    db_conn.commit()


def _own(agent_id: int, **body: object) -> dict[str, object]:
    return {"source": f"agent:{agent_id}", "message": "restarting", "at_turn_end": True, **body}


def _commands(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[object, ...]]:
    db_conn.commit()
    return db_conn.execute(
        "SELECT source, status FROM inbound_messages WHERE agent_id = %s AND kind = 'restart'",
        (agent_id,),
    ).fetchall()


def _harness(db_conn: psycopg.Connection, agent_id: int) -> object:
    db_conn.commit()
    row = db_conn.execute("SELECT harness FROM agents_meta WHERE id = %s", (agent_id,)).fetchone()
    assert row is not None
    return row[0]


def test_a_running_external_agent_may_ask_for_a_switch_and_is_not_touched_yet(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
    db_conn: psycopg.Connection,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        _seed_codex(client, gateway_local_runner.name)
        agent_id = spawn_via_http(
            client, {"config": {"harness": "codex", "llm_model": "gpt-6.1-sol"}}
        ).json()["id"]
        _set_status(db_conn, agent_id, "running")

        answer = client.post(
            f"/api/agents/{agent_id}/restart",
            json=_own(agent_id, config_overlay={"harness": "meiva", "llm_model": "deepseek-flash"}),
        )

    assert answer.status_code == 200
    assert answer.json() == {"status": "enqueued"}
    assert _commands(db_conn, agent_id) == [("self", "pending")]
    assert _harness(db_conn, agent_id) == "codex"


def test_only_the_agent_itself_may_queue_its_restart(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
    db_conn: psycopg.Connection,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        _seed_codex(client, gateway_local_runner.name)
        agent_id = spawn_via_http(
            client, {"config": {"harness": "codex", "llm_model": "gpt-6.1-sol"}}
        ).json()["id"]
        _set_status(db_conn, agent_id, "running")

        for source in ("user", f"agent:{agent_id + 1}", "self"):
            refused = client.post(
                f"/api/agents/{agent_id}/restart",
                json={**_own(agent_id), "source": source},
            )
            assert refused.status_code == 422
            assert "source must be" in refused.json()["detail"]

    assert _commands(db_conn, agent_id) == []


@pytest.mark.parametrize(
    ("overlay", "status", "reason"),
    [
        ({"harness": "meiva"}, 422, "llm_model must be explicitly provided"),
        ({"harness": "claude-code", "llm_model": "x"}, 422, "Invalid harness"),
        ({"harness": "antigravity", "llm_model": "x"}, None, "not launchable"),
        ({"definitely_not_a_config_field": 1}, 422, "definitely_not_a_config_field"),
    ],
    ids=["switch-without-model", "unknown-harness", "unlaunchable-target", "unknown-field"],
)
def test_a_request_that_fails_the_usual_validation_is_refused_at_once_and_records_nothing(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
    db_conn: psycopg.Connection,
    overlay: dict[str, object],
    status: int | None,
    reason: str,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        _seed_codex(client, gateway_local_runner.name)
        agent_id = spawn_via_http(
            client, {"config": {"harness": "codex", "llm_model": "gpt-6.1-sol"}}
        ).json()["id"]
        _set_status(db_conn, agent_id, "running")

        refused = client.post(
            f"/api/agents/{agent_id}/restart", json=_own(agent_id, config_overlay=overlay)
        )

    assert 400 <= refused.status_code < 500
    if status is not None:
        assert refused.status_code == status
    assert reason in refused.json()["detail"]
    assert _commands(db_conn, agent_id) == []
    assert _harness(db_conn, agent_id) == "codex"


def test_a_terminated_agent_cannot_queue_a_restart(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
    db_conn: psycopg.Connection,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        _seed_codex(client, gateway_local_runner.name)
        agent_id = spawn_via_http(
            client, {"config": {"harness": "codex", "llm_model": "gpt-6.1-sol"}}
        ).json()["id"]
        _set_status(db_conn, agent_id, "terminated")

        refused = client.post(f"/api/agents/{agent_id}/restart", json=_own(agent_id))

    assert refused.status_code == 409
    assert "running or idling" in refused.json()["detail"]
    assert _commands(db_conn, agent_id) == []
