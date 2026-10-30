from uuid import UUID

import httpx
import psycopg
import pytest

from base.api_contracts.agent_contract import GatewayUnavailable
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.auth.sdk_transport import AppTransport
from gateway.tests.lifecycle.creation.test_guarded_creation import HEADERS, SECRET
from gateway.tests.lifecycle.creation.test_guarded_creation import runner as runner
from gateway.tests.lifecycle.creation.test_sdk_strong_creation import sdk
from gateway.tests.support.runner_ops.ops_stub import RunnerOps
from meiva.gateway_client import use_client


@pytest.mark.parametrize("rotate", [False, True])
def test_same_sdk_intent_after_lost_response_and_gateway_restart(
    runner: RunnerOps,
    db_conn: psycopg.Connection,
    rotate: bool,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with gateway_test_client(
        cluster_secret=SECRET,
        headers={"Authorization": f"Bearer {SECRET}"},
        machine=machine_identity,
    ) as target:
        wire = AppTransport(target)
        sdk_client = httpx.Client(
            transport=wire, base_url="http://testserver", timeout=httpx.Timeout(10.0)
        )
        with use_client(sdk_client):
            wire.losing_responses = True
            with pytest.raises(GatewayUnavailable):
                sdk()
            lost_attempts = len(wire.requests)
            assert lost_attempts > 1
            wire.losing_responses = False
            row = db_conn.execute(
                "SELECT agent_id, launch_attempt_id FROM agent_creation_snapshots"
            ).fetchone()
            assert row is not None
            agent_id, original = row
            assert runner.payloads("spawn-launch")[0]["launch_attempt_id"] == str(original)
            if rotate:
                retry = target.post(
                    f"/api/keyed/v1/agents/{agent_id}/retry-launch",
                    json={"expected_prior_attempt_id": str(original)},
                    headers={**HEADERS, "Idempotency-Key": "later-retry"},
                )
                assert retry.status_code == 200, retry.text
                assert retry.json()["launch_attempt_id"] != str(original)
    with (
        gateway_test_client(
            cluster_secret=SECRET,
            headers={"Authorization": f"Bearer {SECRET}"},
            machine=machine_identity,
        ) as restarted,
        use_client(sdk_client),
    ):
        wire.target = restarted
        assert sdk() == agent_id
    sdk_client.close()
    assert len(wire.requests) == lost_attempts + 1
    assert len(wire.distinct()) == 1
    launches = runner.payloads("spawn-launch")
    assert len(launches) == (lost_attempts if rotate else lost_attempts + 1)
    assert {launch["launch_attempt_id"] for launch in launches} == {str(original)}
    _assert_one_birth(db_conn, original)


def _assert_one_birth(conn: psycopg.Connection, original: UUID) -> None:
    assert conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    assert conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='spawn'"
    ).fetchone() == (1,)
    assert conn.execute("SELECT launch_attempt_id FROM agent_creation_snapshots").fetchone() == (
        original,
    )
