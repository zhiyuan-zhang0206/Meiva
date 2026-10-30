import httpx
import psycopg
import pytest

from base.api_contracts.agent_contract import GatewayUnavailable
from gateway.tests.auth.gateway_config import GatewayTestClient
from gateway.tests.auth.sdk_transport import AppTransport
from gateway.tests.lifecycle.test_keyed_launch_retry import birth
from gateway.tests.lifecycle.test_keyed_launch_retry import client as client
from gateway.tests.lifecycle.test_keyed_launch_retry import runner as runner
from meiva import agents
from meiva.gateway_client import use_client


def test_sdk_observation_lost_response_and_historical_replay(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    agent_id, prior = birth(client)
    wire = AppTransport(client)
    with (
        httpx.Client(base_url="http://gateway", transport=wire) as http,
        use_client(http),
    ):
        observed = agents.get_launch_attempt(agent_id)
        assert str(observed) == prior
        wire.losing_responses = True
        with pytest.raises(GatewayUnavailable):
            agents.retry_launch(
                agent_id,
                idempotency_key="intent",
                expected_prior_attempt_id=observed,
            )
        assert len([request for request in wire.requests if request.method == "POST"]) > 1
        wire.losing_responses = False
        replacement = agents.get_launch_attempt(agent_id)
        assert replacement != observed
        for _ in range(2):
            assert (
                agents.retry_launch(
                    agent_id,
                    idempotency_key="intent",
                    expected_prior_attempt_id=observed,
                )
                == agent_id
            )
            assert agents.get_launch_attempt(agent_id) == replacement
        with pytest.raises(httpx.HTTPStatusError) as conflict:
            agents.retry_launch(
                agent_id,
                idempotency_key="intent",
                expected_prior_attempt_id=replacement,
            )
        assert conflict.value.response.status_code == 409
        db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent_id,))
        db_conn.commit()
        assert (
            agents.retry_launch(
                agent_id,
                idempotency_key="intent",
                expected_prior_attempt_id=observed,
            )
            == agent_id
        )
    assert db_conn.execute("SELECT count(*) FROM agent_launch_retry_receipts").fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (1,)
