# meiva:note
# `POST /api/agents/{id}/restart` with `at_turn_end`, the agent's own restart: it passes the same
# validation as any restart (a refusal answers the caller at once, nothing is recorded) and then
# the home runner records it as a pending command, which a running agent may ask for although a
# restart sent by anyone else refuses it with 409 (`test_interrupt_compact_routes.py`).
# meiva:link gateway/agents/lifecycle.py

from __future__ import annotations

import psycopg
import pytest

from base.agents.model.harness_models import parse_claude_initialize
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


# meiva:note
# The Fable row as Claude Code 2.1.322 answers `initialize`: Fable appears only as its `[1m]`
# selector. The catalog is built from it by the real parser, as the harness probe builds it.
def _seed_claude_code_from_initialize(client: GatewayTestClient, machine: str) -> None:
    probed = parse_claude_initialize(
        {
            "models": [
                {
                    "value": "claude-fable-5-2[1m]",
                    "resolvedModel": "claude-fable-5-2",
                    "supportsEffort": True,
                    "supportedEffortLevels": ["low", "medium", "high", "max"],
                },
            ]
        }
    )
    client.app.state.harness_cache.store(
        machine,
        [
            HarnessAvailability(
                harness=Harness.CLAUDE_CODE,
                status="available",
                reason="signed in",
                launchable=True,
                billing=HarnessBilling.SUBSCRIPTION,
                models=[
                    HarnessModelOption(
                        id=model.id,
                        reasoning_effort_options=list(model.reasoning_effort_options),
                        reasoning_effort_default=model.reasoning_effort_default,
                    )
                    for model in probed.models
                ],
            )
        ],
    )


# meiva:note
# Every restart of an external agent re-checks its stored model against the live catalog, so an
# agent pinned to the bare `claude-fable-5-2` its CLI runs must pass that check even when the
# overlay names no model at all.
def test_a_claude_code_agent_on_the_bare_fable_id_restarts_with_an_unrelated_overlay(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
    db_conn: psycopg.Connection,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        _seed_claude_code_from_initialize(client, gateway_local_runner.name)
        spawned = spawn_via_http(
            client, {"config": {"harness": "claude_code", "llm_model": "claude-fable-5-2"}}
        )
        assert spawned.status_code == 201, spawned.text
        agent_id = spawned.json()["id"]
        _set_status(db_conn, agent_id, "running")

        answer = client.post(
            f"/api/agents/{agent_id}/restart",
            json=_own(agent_id, config_overlay={"heartbeat_pause_max_seconds": 604800}),
        )

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"status": "enqueued"}
    assert _commands(db_conn, agent_id) == [("self", "pending")]
    assert _harness(db_conn, agent_id) == "claude_code"


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


@pytest.mark.parametrize("status", ["running", "idling", "terminated"])
def test_self_appointment_never_wakes_at_submission(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
    db_conn: psycopg.Connection,
    status: str,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        agent_id = spawn_via_http(client).json()["id"]
        _set_status(db_conn, agent_id, status)
        before = db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
        ).fetchone()
        answer = client.post(
            f"/api/agents/{agent_id}/restart",
            params={"at": "+60s"},
            json={"source": f"agent:{agent_id}", "message": "switch later"},
        )
        assert answer.status_code == 200, answer.text
        receipt = answer.json()
        schedule = client.get(f"/api/schedules/{receipt['schedule_id']}").json()
        assert schedule["owner_agent_id"] is None
        assert schedule["trigger_kind"] == "at"
        assert "--scheduled-attempt" in schedule["script"]
        db_conn.commit()
        assert (
            db_conn.execute(
                "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
            ).fetchone()
            == before
        )
        assert _commands(db_conn, agent_id) == []


def test_scheduled_native_restart_refuses_running_without_interrupt(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
    db_conn: psycopg.Connection,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        agent_id = spawn_via_http(client).json()["id"]
        _set_status(db_conn, agent_id, "running")
        answer = client.post(
            f"/api/agents/{agent_id}/restart/scheduled-attempt",
            json={"source": f"agent:{agent_id}", "message": "later"},
        )
        assert answer.status_code == 409, answer.text
        assert _commands(db_conn, agent_id) == []
        db_conn.commit()
        assert db_conn.execute(
            "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone() == ("running",)


def test_appointment_requires_source_and_valid_config(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        agent_id = spawn_via_http(client).json()["id"]
        for body in (
            {"message": "later"},
            {"source": "user", "message": "later", "config_overlay": {"harness": "invalid"}},
        ):
            answer = client.post(
                f"/api/agents/{agent_id}/restart", params={"at": "+60s"}, json=body
            )
            assert answer.status_code == 422, answer.text


@pytest.mark.parametrize("status", ["running", "idling", "terminated"])
def test_peer_appointment_warns_only_a_running_target(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
    db_conn: psycopg.Connection,
    status: str,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        agent_id = spawn_via_http(client).json()["id"]
        _set_status(db_conn, agent_id, status)
        before = db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
        ).fetchone()
        answer = client.post(
            f"/api/agents/{agent_id}/restart",
            params={"at": "+60s"},
            json={"source": "user", "message": "wrap up"},
        )
        assert answer.status_code == 200, answer.text
        db_conn.commit()
        after = db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
        ).fetchone()
        assert before is not None and after is not None
        assert after[0] - before[0] == int(status == "running")


def test_external_restart_without_overlay_probes_launchability(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        _seed_codex(client, gateway_local_runner.name)
        agent_id = spawn_via_http(
            client, {"config": {"harness": "codex", "llm_model": "gpt-6.1-sol"}}
        ).json()["id"]
        client.app.state.harness_cache.store(
            gateway_local_runner.name,
            [
                HarnessAvailability(
                    harness=Harness.CODEX,
                    status="unavailable",
                    reason="login expired",
                    launchable=False,
                    billing=HarnessBilling.SUBSCRIPTION,
                    models=[],
                )
            ],
        )
        before = len(gateway_local_runner.calls)
        answer = client.post(
            f"/api/agents/{agent_id}/restart", json={"source": "user", "message": "restart"}
        )
        assert answer.status_code == 400, answer.text
        assert "login expired" in answer.text
        assert len(gateway_local_runner.calls) == before


@pytest.mark.parametrize(
    "at",
    [
        "+999999999999999999999d",
        "+3650000d",
        "9999-12-31T23:59:59.999999-23:59",
    ],
)
def test_appointment_time_overflow_refuses_without_queueing(
    _local_machine: MachineIdentityReader,
    gateway_local_runner: RunnerOps,
    db_conn: psycopg.Connection,
    at: str,
) -> None:
    with gateway_test_client(machine=_local_machine) as client:
        agent_id = spawn_via_http(client).json()["id"]
        before = db_conn.execute("SELECT count(*) FROM schedules").fetchone()
        answer = client.post(
            f"/api/agents/{agent_id}/restart",
            params={"at": at},
            json={"source": f"agent:{agent_id}", "message": "invalid time"},
        )
        assert answer.status_code == 422, answer.text
        db_conn.commit()
        assert db_conn.execute("SELECT count(*) FROM schedules").fetchone() == before
        assert _commands(db_conn, agent_id) == []
