from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

import meiva.agents
from base.api_contracts.agent_contract import ForkHarnessMismatch
from gateway.tests.auth.sdk_transport import AppTransport
from gateway.tests.lifecycle.creation.test_guarded_creation import client as client
from gateway.tests.lifecycle.creation.test_guarded_creation import runner as runner
from gateway.tests.lifecycle.creation.test_sdk_strong_creation import wire as wire
from meiva.tests.fixtures.sdk import sdk_environment as sdk_environment


def _checkpoint(conn: psycopg.Connection, agent_id: int) -> None:
    conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint, metadata) "
        "VALUES (%s, %s, %s, %s)",
        (str(agent_id), "ckpt-1", Jsonb({"channel_versions": {}}), Jsonb({})),
    )
    conn.commit()


def _harness(conn: psycopg.Connection, agent_id: int) -> str:
    row = conn.execute("SELECT harness FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
    assert row is not None
    return row[0]


def test_sdk_fork_without_a_harness_keeps_the_source_harness(
    wire: AppTransport, db_conn: psycopg.Connection, sdk_environment: object
) -> None:
    source = meiva.agents.spawn(
        prompt="one goal",
        machine="local-test",
        harness="meiva",
        idempotency_key="sdk-harness-source",
    )
    _checkpoint(db_conn, source)
    child = meiva.agents.spawn(
        fork_from=source, machine="local-test", idempotency_key="sdk-harness-fork"
    )
    assert _harness(db_conn, source) == "meiva"
    assert _harness(db_conn, child) == "meiva"
    assert len(wire.requests) == 2


def test_sdk_fork_of_an_external_harness_raises_fork_harness_mismatch(
    wire: AppTransport, db_conn: psycopg.Connection, sdk_environment: object
) -> None:
    source = meiva.agents.spawn(
        prompt="one goal",
        machine="local-test",
        harness="codex",
        idempotency_key="sdk-ext-harness-source",
    )
    _checkpoint(db_conn, source)
    with pytest.raises(ForkHarnessMismatch, match="only supported on the native 'meiva' harness"):
        meiva.agents.spawn(
            fork_from=source, machine="local-test", idempotency_key="sdk-ext-harness-fork"
        )
    assert _harness(db_conn, source) == "codex"
    assert len(wire.requests) == 2
