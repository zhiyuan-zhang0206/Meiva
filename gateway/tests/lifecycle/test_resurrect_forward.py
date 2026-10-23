# meiva:note
# Cross-machine resurrect forward (`gateway/agents/lifecycle.py:post_agent_resurrect`) tests —
#
# resurrect must forward to the home machine (a session spawn starting a new process
# is a physical local-machine operation). Forward has no local shortcut: even when
# the target is the local machine, the ops server executes in-process on localhost.
# This endpoint covers the scenario auto-resurrect can't — a pure wake with no message
# to deliver (frontend resurrect button).
#
# The home machine is a runner served on a real listener (`gateway/tests/support/runner_ops`) and
# registered in `machines`. The lifecycle dispatch budget is `GatewayConfig.lifecycle_*`.

from __future__ import annotations

import time

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base.api_contracts.agent_contract import CrossMachineGatewayUnavailable
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from gateway.agents import lifecycle as lifecycle_module
from gateway.agents.launch import forward
from gateway.http.config import GatewayConfig
from gateway.tests.auth.gateway_config import (
    gateway_test_client,
    gateway_test_config,
    spawn_via_http,
)
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    RunnerOps,
    completed,
    failed,
    refused,
    runner_ops,
)


@pytest.fixture
def local_machine(
    set_machine_identity: MachineIdentitySetter, gateway_local_runner: RunnerOps
) -> MachineIdentity:
    """The gateway's own machine, serving as an agent-runner through its in-process ops route."""
    return set_machine_identity(role="agent-runner")


def _set_agent_machine(db_conn: psycopg.Connection, agent_id: int, machine: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine = %s, status = 'terminated' WHERE id = %s",
            (machine, agent_id),
        )
    db_conn.commit()


class TestResurrectRouting:
    def test_remote_machine_forwards(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        """machine != local → forward entire body to home machine's ops server,
        local does not touch DB / does not spawn a session."""
        with (
            runner_ops(db_conn, "remote-mac", {"lifecycle": completed({"status": "spawned"})}) as (
                runner
            ),
            gateway_test_client(machine=local_machine) as client,
        ):
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            resp = client.post(
                f"/api/agents/{agent_id}/resurrect",
                json={"resurrected_by": "user", "prompt": "hello"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "spawned"}
        (call,) = runner.calls_of("lifecycle")
        assert call.payload["path"] == f"/api/agents/{agent_id}/resurrect-explicit"
        assert call.payload["body"]["resurrected_by"] == "user"
        assert call.payload["body"]["prompt"] == "hello"
        status = db_conn.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        assert status.fetchone() == ("terminated",)

    def test_bare_resurrect_forwards_with_defaults(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        """Bare lifecycle wake with no body (frontend resurrect button) → forward,
        prompt=None, resurrected_by defaults to 'user'. This is the scenario
        auto-resurrect cannot cover."""
        with (
            runner_ops(db_conn, "remote-mac", {"lifecycle": completed({"status": "spawned"})}) as (
                runner
            ),
            gateway_test_client(machine=local_machine) as client,
        ):
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            resp = client.post(f"/api/agents/{agent_id}/resurrect")
        assert resp.status_code == 200
        assert resp.json() == {"status": "spawned"}
        (body,) = [payload["body"] for payload in runner.payloads("lifecycle")]
        assert body["prompt"] is None
        assert body["resurrected_by"] == "user"

    # meiva:note
    # A failed op that names a wire reason is re-raised as that error on the gateway.
    def test_a_runners_wire_error_keeps_its_status_and_reason(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        refusal = failed(
            {
                "error": "MachineNotRegistered: remote-mac",
                "detail": "machine 'remote-mac' not found in machines table",
                "reason": "machine_not_registered",
            }
        )
        with (
            runner_ops(db_conn, "remote-mac", {"lifecycle": refusal}),
            gateway_test_client(machine=local_machine) as client,
        ):
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            resp = client.post(f"/api/agents/{agent_id}/resurrect")
        assert resp.status_code == 404
        assert resp.json()["reason"] == "machine_not_registered"

    # meiva:note
    # A home machine the registry has never seen cannot be dialed: 502, named.
    def test_an_unregistered_home_machine_is_a_gateway_error(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client(machine=local_machine) as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "ghost-mac")
            resp = client.post(f"/api/agents/{agent_id}/resurrect")
        assert resp.status_code == 502
        assert resp.json()["reason"] == "cross_machine_gateway_unavailable"
        assert "ghost-mac" in resp.json()["detail"]

    def test_cross_machine_gateway_unavailable_propagates_502(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        """target ops server transport error → CrossMachineGatewayUnavailable → 502."""
        with (
            runner_ops(db_conn, "remote-mac", {"lifecycle": refused(404, "no ops server here")}),
            gateway_test_client(machine=local_machine) as client,
        ):
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            resp = client.post(f"/api/agents/{agent_id}/resurrect")
        assert resp.status_code == 502
        assert resp.json()["reason"] == "cross_machine_gateway_unavailable"

    def test_nonexistent_agent_404(self, local_machine: MachineIdentity) -> None:
        """Nonexistent → helper's own SELECT hits AgentNotFound (404), does not query machines table."""
        with gateway_test_client(machine=local_machine) as client:
            resp = client.post("/api/agents/9999/resurrect")
        assert resp.status_code == 404


# meiva:note
# Even when agents_meta.machine is local, resurrect still goes through the dial —
# no local shortcut. Single box is just the special case where the ops server
# happens to be on localhost.
def test_local_home_machine_is_dialed_like_any_other(
    local_machine: MachineIdentity,
    gateway_local_runner: RunnerOps,
    db_conn: psycopg.Connection,
) -> None:
    local = gateway_local_runner.name
    with gateway_test_client(machine=local_machine) as client:
        agent_id = spawn_via_http(client, {}).json()["id"]
        _set_agent_machine(db_conn, agent_id, local)
        with runner_ops(
            db_conn, local, {"lifecycle": completed({"status": "spawned"})}
        ) as scripted:
            resp = client.post(f"/api/agents/{agent_id}/resurrect")
    assert resp.status_code == 200
    assert resp.json() == {"status": "spawned"}
    (call,) = scripted.calls_of("lifecycle")
    assert call.payload["path"] == f"/api/agents/{agent_id}/resurrect-explicit"


# meiva:note
# An unreachable remote host must produce a gateway error before the caller's request
# deadline, without giving up retry-safe lifecycle dispatch.
def test_lifecycle_budget_defaults_are_short_and_retry_safe() -> None:
    config = gateway_test_config()
    assert config.lifecycle_attempt_timeout_seconds < 15.0
    assert config.lifecycle_deadline_seconds < 15.0
    assert config.lifecycle_retries == 1


# meiva:note
# A runner that fails once answers the retry: one extra attempt, the same dedup key.
@pytest.mark.asyncio
async def test_lifecycle_forward_retries_a_transient_failure_once_under_one_key(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    attempts: list[OpsCall] = []

    def flaky(call: OpsCall) -> OpsReply:
        attempts.append(call)
        return refused(503, "restarting") if len(attempts) == 1 else completed({"status": "ok"})

    with runner_ops(db_conn, "flaky-runner", {"lifecycle": flaky}):
        result = await forward.enqueue_lifecycle(
            Database.from_settings(gate=database_gate),
            "flaky-runner",
            "/restart",
            {},
            config=gateway_test_config(),
        )

    assert result == {"status": "ok"}
    assert len(attempts) == 2
    assert attempts[0].idempotency_key == attempts[1].idempotency_key
    assert attempts[0].idempotency_key is not None


# meiva:note
# A runner that never recovers costs `lifecycle_retries + 1` attempts, then a named 502.
@pytest.mark.asyncio
async def test_lifecycle_forward_gives_up_after_its_retry_budget(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    with (
        runner_ops(db_conn, "down-runner", {"lifecycle": refused(503, "down")}) as runner,
        pytest.raises(CrossMachineGatewayUnavailable, match="down-runner"),
    ):
        await forward.enqueue_lifecycle(
            Database.from_settings(gate=database_gate),
            "down-runner",
            "/restart",
            {},
            config=gateway_test_config(lifecycle_retries=2),
        )
    assert len(runner.calls_of("lifecycle")) == 3


# meiva:note
# A runner that accepts the connection and never answers is still bounded at the
# router boundary, where the user receives a named 502 instead of silence.
@pytest.mark.asyncio
async def test_lifecycle_forward_deadline_becomes_a_clear_gateway_error(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    deadline = 0.3

    def silent(call: OpsCall) -> OpsReply:
        time.sleep(deadline * 4)
        return completed({})

    config: GatewayConfig = gateway_test_config(lifecycle_deadline_seconds=deadline)
    started = time.monotonic()
    with (
        runner_ops(db_conn, "silent-runner", {"lifecycle": silent}),
        pytest.raises(CrossMachineGatewayUnavailable, match="did not answer"),
    ):
        await forward.enqueue_lifecycle(
            Database.from_settings(gate=database_gate),
            "silent-runner",
            "/restart",
            {},
            config=config,
        )
    assert time.monotonic() - started < deadline * 4


# meiva:note
# Home lookup and RPC use the resources on the serving app's state, nothing ambient:
# an app that carries only a database, its pool and a config can serve the route.
def test_resurrect_needs_only_the_serving_apps_resources(
    database: Database, db_conn: psycopg.Connection
) -> None:
    from base.db import create_agent

    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id, status, machine) VALUES (%s, 'terminated', %s)",
        (agent_id, "request-owned-home"),
    )
    db_conn.commit()
    with (
        runner_ops(
            db_conn, "request-owned-home", {"lifecycle": completed({"status": "spawned"})}
        ) as runner,
        database.pool(max_size=2) as pool,
    ):
        serving_app = FastAPI()
        serving_app.state.db = database
        serving_app.state.db_pool = pool
        serving_app.state.gateway_config = gateway_test_config()
        serving_app.include_router(lifecycle_module.router)
        with TestClient(serving_app) as client:
            response = client.post(f"/api/agents/{agent_id}/resurrect")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "spawned"}
    (call,) = runner.calls_of("lifecycle")
    assert call.payload["path"] == f"/api/agents/{agent_id}/resurrect-explicit"
    assert call.payload["body"]["resurrected_by"] == "user"
