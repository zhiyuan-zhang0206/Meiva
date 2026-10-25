# meiva:note
# Cross-machine spawn forward (`gateway/agents/router.py:post_agents`) tests.
#
# A spawn commits the agent row in the gateway and then forwards a `spawn-launch` op to the
# target machine's ops server. The target is a runner served on a real listener
# (`gateway/tests/support/runner_ops`) and registered in `machines`, so the registry reads, the dispatch
# and the result handling all run; a test asserts the HTTP response and the envelopes the runner
# received. The pure decisions are asserted in `gateway/agents/tests/test_launch_plan.py`.

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from base.agents.observation.evidence import AvailabilityReason
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.packages.plugin_providers import build_model_catalog
from gateway.agents import creation
from gateway.agents.launch import forward
from gateway.agents.launch.launch_plan import LaunchForwardError
from gateway.tests.auth.gateway_config import gateway_test_client, spawn_via_http
from gateway.tests.support.runner_ops.ops_stub import (
    completed,
    failed,
    launched,
    refused,
    register_machine,
    runner_ops,
)
from ops.rpc_schemas import LaunchAgentRequest


@pytest.fixture
def _force_local_machine(set_machine_identity: MachineIdentitySetter) -> MachineIdentityReader:
    """Sets this unit's identity at the source via set_machine_identity so every
    machine_name() / machine_role() call site sees role=agent-runner, name='local-test'."""
    return set_machine_identity(role="agent-runner", name="local-test")


class TestRouting:
    def test_machine_eq_local_forwards_to_local_target(
        self, _force_local_machine: MachineIdentityReader, db_conn: psycopg.Connection
    ) -> None:
        """body.machine == local → spawn is HTTP-uniform: still forwarded, with
        target == local (the co-located runner's ops server over localhost)."""
        db_conn.execute(
            "INSERT INTO machine_probe (machine_name,online,agent_host_online) "
            "VALUES ('local-test',TRUE,FALSE)"
        )
        with (
            runner_ops(db_conn, "local-test", {"spawn-launch": launched}) as runner,
            gateway_test_client(machine=_force_local_machine) as client,
        ):
            resp = spawn_via_http(client, {"machine": "local-test"})
        assert resp.status_code == 201
        assert [call.kind for call in runner.calls] == ["spawn-launch"]
        assert resp.json()["id"] == runner.calls[0].payload["agent_id"]
        assert resp.json()["accepted"] is True
        assert resp.json()["execution_observed"] is False
        assert resp.json()["reason"] == "host_unavailable"
        assert resp.json()["observed_at"]

    def test_machine_none_forwards_to_local_target(
        self, _force_local_machine: MachineIdentityReader, db_conn: psycopg.Connection
    ) -> None:
        """body.machine absent → equivalent to local; still forwarded, target == local."""
        with (
            runner_ops(db_conn, "local-test", {"spawn-launch": launched}),
            gateway_test_client(machine=_force_local_machine) as client,
        ):
            resp = spawn_via_http(client, {})
        assert resp.status_code == 201

    def test_local_gateway_only_target_400(
        self, set_machine_identity: MachineIdentitySetter, db_conn: psycopg.Connection
    ) -> None:
        """A target the registry resolves to gateway-only (no agent-runner
        capability) returns 400 + reason='spawn_target_not_agent_runner' via the
        wire contract (a bare HTTPException would carry no reason and trip the SDK
        into a confusing KeyError), and never forwards. The check reads the
        registry for the local target too — no self-introspection."""
        machine = set_machine_identity(role="gateway", name="gw-only")
        register_machine(db_conn, "gw-only", roles=("gateway",))
        with gateway_test_client(machine=machine) as client:
            resp = spawn_via_http(client, {})
        assert resp.status_code == 400
        body = resp.json()
        assert body["reason"] == "spawn_target_not_agent_runner"
        assert "agent-runner" in body["detail"]
        assert db_conn.execute("SELECT count(*) FROM agents_meta").fetchone() == (
            0,
            # meiva:note
            # the guard fires before the row exists
        )

    def test_paused_target_409_no_forward(
        self, _force_local_machine: MachineIdentityReader, db_conn: psycopg.Connection
    ) -> None:
        """A spawn targeting a PAUSED machine (operator `meiva cluster pause`) is
        refused with 409 + reason='machine_paused' before any forward — the
        paused machine is deliberately out of the cluster (agents terminated,
        ops server may be unreachable), so a spawn would fail at dial time
        anyway; the precise wire error is what schedules / peers see."""
        with (
            runner_ops(db_conn, "paused-box", {"spawn-launch": launched}, paused=True),
            gateway_test_client(machine=_force_local_machine) as client,
        ):
            resp = spawn_via_http(client, {"machine": "paused-box"})
        assert resp.status_code == 409
        body = resp.json()
        assert body["reason"] == "machine_paused"
        assert "resume" in body["detail"]

    # meiva:note
    # body.machine != local → the launch op reaches that machine, body passthrough.
    def test_machine_neq_local_forwards(
        self, _force_local_machine: MachineIdentityReader, db_conn: psycopg.Connection
    ) -> None:
        with (
            runner_ops(db_conn, "remote-mac", {"spawn-launch": launched}) as runner,
            gateway_test_client(machine=_force_local_machine) as client,
        ):
            resp = spawn_via_http(client, {"machine": "remote-mac", "spawner": "user"})
        assert resp.status_code == 201
        assert resp.json()["accepted"] is True
        assert resp.json()["execution_observed"] is False
        assert resp.json()["reason"] == "unknown"
        # The forward op is the launch half of the #1236 split — the row was
        # already created by the gateway, so the body carries the new agent id,
        # not the REST request (which has no machine / spawner on the wire op).
        (launch,) = runner.calls_of("spawn-launch")
        assert resp.json()["id"] == launch.payload["agent_id"] > 0
        assert "config" not in launch.payload
        assert "prompt" not in launch.payload
        assert launch.payload["launch_attempt_id"]
        assert launch.idempotency_key is not None
        assert launch.idempotency_key.startswith("spawn-launch:remote-mac:")

    def test_machine_not_registered_propagates_404(
        self, _force_local_machine: MachineIdentityReader, db_conn: psycopg.Connection
    ) -> None:
        """A target not in the machines table → the pre-dispatch lookup_role raises
        MachineNotRegistered → the app's MeivaAgentError handler emits 404 + reason
        (no opaque 502 from dialing a non-existent ops server).

        Wire protocol: MeivaAgentError subclass → JSONResponse status=cls.http_status,
        body={detail, reason}. MachineNotRegistered.http_status=404.
        """
        with gateway_test_client(machine=_force_local_machine) as client:
            resp = spawn_via_http(client, {"machine": "ghost-mac"})
        assert resp.status_code == 404
        body = resp.json()
        assert body["reason"] == "machine_not_registered"
        assert "ghost-mac" in body["detail"]

    # meiva:note
    # A post-commit forward error returns 502 with the committed identity.
    def test_unreachable_runner_names_committed_agent(
        self, _force_local_machine: MachineIdentityReader, db_conn: psycopg.Connection
    ) -> None:
        with (
            runner_ops(db_conn, "remote-mac", {"spawn-launch": refused(404, "gone")}),
            gateway_test_client(machine=_force_local_machine) as client,
        ):
            resp = spawn_via_http(client, {"machine": "remote-mac"})
        assert resp.status_code == 502
        assert resp.json()["reason"] == "agent_launch_failed"
        assert resp.json()["agent_id"] > 0
        assert resp.json()["state"]["availability"]["reason"] == "launch_unreachable"

    def test_mismatched_runner_receipt_cannot_replace_committed_id(
        self, _force_local_machine: MachineIdentityReader, db_conn: psycopg.Connection
    ) -> None:
        with (
            runner_ops(
                db_conn,
                "local-test",
                {"spawn-launch": lambda call: completed({"id": call.payload["agent_id"] + 1})},
            ),
            gateway_test_client(machine=_force_local_machine) as client,
        ):
            response = spawn_via_http(client, {})
        assert response.status_code == 502
        body = response.json()
        assert body["state"]["availability"]["reason"] == "launch_unreachable"
        assert body["agent_id"] > 0


@pytest.mark.asyncio
async def test_spawn_forward_sends_the_launch_kind_and_classifies_an_unreachable_runner(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    attempt_id = uuid4()
    with (
        runner_ops(db_conn, "runner", {"spawn-launch": refused(404, "offline")}) as runner,
        pytest.raises(LaunchForwardError) as raised,
    ):
        await forward.forward_spawn_to_remote(
            Database.from_settings(gate=database_gate),
            "runner",
            LaunchAgentRequest(agent_id=4, launch_attempt_id=attempt_id),
        )
    assert [(call.kind, call.payload) for call in runner.calls] == [
        ("spawn-launch", {"agent_id": 4, "launch_attempt_id": str(attempt_id)})
    ]
    assert raised.value.reason == AvailabilityReason.LAUNCH_UNREACHABLE


@pytest.mark.asyncio
async def test_spawn_forward_preserves_runner_rejection_detail(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    rejection = failed(
        {
            "error": "InvalidModelConfig: key missing",
            "reason": "invalid_model_config",
            "detail": "key missing",
        }
    )
    with (
        runner_ops(db_conn, "runner", {"spawn-launch": rejection}),
        pytest.raises(LaunchForwardError) as raised,
    ):
        await forward.forward_spawn_to_remote(
            Database.from_settings(gate=database_gate),
            "runner",
            LaunchAgentRequest(agent_id=4, launch_attempt_id=uuid4()),
        )
    assert raised.value.reason == AvailabilityReason.LAUNCH_REJECTED
    assert "invalid_model_config: key missing" in str(raised.value)


# meiva:note
# A receipt that cannot be read (the store fails, or the row is gone) is reported as
# `unknown`; it never turns an accepted creation into a failure.
@pytest.mark.parametrize("fault", ["unopened-pool", "unknown-agent"])
def test_receipt_read_failure_keeps_accepted_creation(fault: str, database: Database) -> None:
    with database.pool(max_size=1) as pool:
        unreadable = (
            ConnectionPool[Connection[TupleRow]]("postgresql://127.0.0.1:1/none", open=False)
            if fault == "unopened-pool"
            else pool
        )
        availability = creation.creation_availability(
            unreadable,
            987654,
            catalog=build_model_catalog(),
            default_model="model",
        )
    assert availability.reason == AvailabilityReason.UNKNOWN
    assert availability.observed_at


@pytest.fixture(autouse=True)
def _restore_cold_gateway_delivery(unit_home: Path) -> Iterator[None]:
    """Declare the provider credential on the unit's own `.env` channel for the real lifespan."""
    env_file = unit_home / ".env"
    prior = env_file.read_text(encoding="utf-8") if env_file.exists() else None
    env_file.write_text((prior or "") + "\nDEEPSEEK_API_KEY=test-key\n", encoding="utf-8")
    try:
        yield
    finally:
        if prior is None:
            env_file.unlink(missing_ok=True)
        else:
            env_file.write_text(prior, encoding="utf-8")
