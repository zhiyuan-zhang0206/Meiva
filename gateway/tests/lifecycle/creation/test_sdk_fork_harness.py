from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import psycopg
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

import meiva.agents
from gateway.tests.lifecycle.creation.test_guarded_creation import client as client
from gateway.tests.lifecycle.creation.test_sdk_strong_creation import httpx_response_of
from meiva.gateway_client import use_client
from meiva.tests.fixtures.sdk import sdk_environment as sdk_environment


def _sdk_http(client: TestClient) -> MagicMock:
    http = MagicMock()

    def submit(path: str, **kwargs: Any) -> Any:
        return httpx_response_of(client.post(path, **kwargs))

    http.post.side_effect = submit
    return http


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
    client: TestClient, db_conn: psycopg.Connection, sdk_environment: object
) -> None:
    with use_client(_sdk_http(client)):
        source = meiva.agents.spawn(
            prompt="one goal",
            machine="local-test",
            harness="codex",
            idempotency_key="sdk-harness-source",
        )
        _checkpoint(db_conn, source)
        child = meiva.agents.spawn(
            fork_from=source, machine="local-test", idempotency_key="sdk-harness-fork"
        )
    assert _harness(db_conn, source) == "codex"
    assert _harness(db_conn, child) == "codex"
