import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.api_contracts.agent_contract import GatewayUnavailable
from gateway.tests.auth.gateway_config import GatewayTestClient
from gateway.tests.auth.sdk_transport import AppTransport
from gateway.tests.auth.sdk_transport import zero_retry_delay as zero_retry_delay
from gateway.tests.lifecycle.creation.test_guarded_creation import HEADERS, PATH
from gateway.tests.lifecycle.creation.test_guarded_creation import client as client
from gateway.tests.lifecycle.creation.test_guarded_creation import runner as runner
from gateway.tests.lifecycle.creation.test_sdk_strong_creation import sdk
from gateway.tests.lifecycle.creation.test_sdk_strong_creation import wire as wire


def _checkpoint(conn: psycopg.Connection, agent_id: int, checkpoint_id: str) -> None:
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint, metadata) "
        "VALUES (%s,%s,%s,%s)",
        (str(agent_id), checkpoint_id, Jsonb({"channel_versions": {}}), Jsonb({})),
    )
    conn.commit()


@pytest.mark.parametrize("source_change", ["advanced", "empty"])
def test_sdk_fork_lost_response_retains_original_child_and_checkpoint(
    zero_retry_delay: None,
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    wire: AppTransport,
    source_change: str,
) -> None:
    parent = client.post(
        PATH, json={"machine": "local-test"}, headers={**HEADERS, "Idempotency-Key": "parent"}
    )
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["id"]
    _checkpoint(db_conn, parent_id, "original-checkpoint")
    wire.losing_responses = True
    with pytest.raises(GatewayUnavailable):
        sdk(fork_from=parent_id, idempotency_key="fork-intent")
    lost_attempts = len(wire.requests)
    assert lost_attempts > 1
    wire.losing_responses = False
    row = db_conn.execute(
        "SELECT id, fork_source_agent_id, fork_source_checkpoint_id, last_launch_attempt_id "
        "FROM agents_meta WHERE fork_source_agent_id=%s",
        (parent_id,),
    ).fetchone()
    assert row is not None
    child_id, source_id, checkpoint_id, attempt_id = row
    assert (source_id, checkpoint_id) == (parent_id, "original-checkpoint")
    # meiva:note
    # The source moves on (a later checkpoint) or loses its checkpoints altogether, which
    # would refuse a fresh fork at preflight; the replay must still return the original child.
    if source_change == "advanced":
        _checkpoint(db_conn, parent_id, "later-checkpoint")
    else:
        db_conn.execute("DELETE FROM checkpoints WHERE thread_id=%s", (str(parent_id),))
        db_conn.commit()
    assert sdk(fork_from=parent_id, idempotency_key="fork-intent") == child_id

    assert len(wire.requests) == lost_attempts + 1
    assert len(wire.distinct()) == 1
    assert db_conn.execute(
        "SELECT checkpoint_id FROM checkpoints WHERE thread_id=%s", (str(child_id),)
    ).fetchall() == [("original-checkpoint",)]
    assert db_conn.execute(
        "SELECT kind FROM inbound_messages WHERE agent_id=%s ORDER BY id", (child_id,)
    ).fetchall() == [("fork",), ("chat",)]
    assert db_conn.execute(
        "SELECT launch_attempt_id FROM agent_creation_snapshots WHERE agent_id=%s", (child_id,)
    ).fetchone() == (attempt_id,)
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (2,)
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='fork'"
    ).fetchone() == (1,)
