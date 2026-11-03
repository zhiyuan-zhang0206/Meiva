from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.api_contracts.agent_contract import Harness, HarnessBilling
from gateway.schemas.models import HarnessAvailability, HarnessModelOption
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    spawn_via_http,
)
from gateway.tests.support.runner_ops.ops_stub import completed, launched, runner_ops

pytestmark = pytest.mark.usefixtures("gateway_local_runner")


def _seed_harnesses(client: GatewayTestClient) -> None:
    machine = client.app.state.machine.name()
    client.app.state.harness_cache.store(
        machine,
        [
            HarnessAvailability(
                harness=Harness.MEIVA,
                status="available",
                reason="built-in",
                launchable=True,
                billing=HarnessBilling.API,
            ),
            HarnessAvailability(
                harness=Harness.ANTIGRAVITY,
                status="available",
                reason="signed in",
                launchable=True,
                billing=HarnessBilling.SUBSCRIPTION,
                models=[
                    HarnessModelOption(
                        id="gemini-4-argon",
                        reasoning_effort_options=["low", "medium", "high"],
                        reasoning_effort_default="high",
                    ),
                    HarnessModelOption(
                        id="gpt-oss-120b",
                        reasoning_effort_options=["medium"],
                        reasoning_effort_default="medium",
                    ),
                ],
                default_model="gemini-4-argon",
            ),
            HarnessAvailability(
                harness=Harness.CODEX,
                status="available",
                reason="signed in",
                launchable=True,
                billing=HarnessBilling.SUBSCRIPTION,
                models=[
                    HarnessModelOption(
                        id="gpt-6.1-terra",
                        reasoning_effort_options=["low", "medium", "high", "ultra"],
                        reasoning_effort_default="low",
                    )
                ],
                default_model="gpt-6.1-terra",
            ),
            HarnessAvailability(
                harness=Harness.KIMI_CODE,
                status="unavailable",
                reason="not signed in",
                launchable=False,
                billing=HarnessBilling.SUBSCRIPTION,
            ),
        ],
    )


def _checkpoint(conn: psycopg.Connection, agent_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, "
            "checkpoint, metadata) VALUES (%s, '', 'ckpt-1', 'x', '{}'::jsonb, '{}'::jsonb)",
            (str(agent_id),),
        )
    conn.commit()
    return "ckpt-1"


def _harness(conn: psycopg.Connection, agent_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT harness FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _agent_count(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agents")
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _newest_agent(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT max(id) FROM agents")
        row = cur.fetchone()
    assert row is not None and row[0] is not None
    return int(row[0])


def _spawn(client: TestClient, body: dict[str, object]) -> int:
    # meiva:note
    # These cases used to reach the row through a launch the runner refused, with
    # antigravity as the standing example of a harness without a launcher. The release
    # that gave antigravity its launcher removed that example, so the row is reached
    # the ordinary way; the refused-launch receipt keeps its own coverage in
    # `tests/lifecycle/test_spawn_forward.py` and `tests/test_agents_endpoints.py`.
    # meiva:link base/agents/docs/harness/harness.meiva.okf.md
    response = spawn_via_http(client, {"spawner": "user", **body})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


class TestSpawnHarness:
    def test_spawn_records_the_requested_harness(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            _seed_harnesses(client)
            agent = _spawn(
                client,
                {
                    "harness": "antigravity",
                    "config": {"llm_model": "gemini-4-argon", "reasoning_effort": "high"},
                },
            )
        assert _harness(db_conn, agent) == "antigravity"

    def test_uncatalogued_probed_model_spawns_on_external_harness(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            _seed_harnesses(client)
            agent = _spawn(
                client,
                {
                    "harness": "antigravity",
                    "config": {"llm_model": "gpt-oss-120b", "reasoning_effort": "medium"},
                },
            )
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent,))
            row = cur.fetchone()
        assert row is not None
        assert row[0]["llm_model"] == "gpt-oss-120b"
        assert row[0]["reasoning_effort"] == "medium"

    def test_cold_cache_external_spawn_probes_runner_once_and_stamps_defaults(
        self, db_conn: psycopg.Connection
    ) -> None:
        probe_payload = {
            "machine": "remote-probe",
            "harnesses": [
                {"harness": "meiva", "status": "available", "reason": "built-in"},
                {
                    "harness": "codex",
                    "status": "available",
                    "reason": "signed in",
                    "models": [
                        {
                            "id": "gpt-6.1-terra",
                            "reasoning_effort_options": ["low", "high", "ultra"],
                            "reasoning_effort_default": "low",
                        }
                    ],
                    "default_model": "gpt-6.1-terra",
                },
            ],
        }
        replies = {
            "harness_probe": completed(probe_payload),
            "spawn-launch": launched,
            "launch-reconcile": completed({"wake_published": False}),
        }
        with (
            runner_ops(db_conn, "remote-probe", replies) as remote,
            gateway_test_client() as client,
        ):
            first = _spawn(client, {"machine": "remote-probe", "harness": "codex"})
            second = _spawn(
                client,
                {
                    "machine": "remote-probe",
                    "harness": "codex",
                    "config": {"llm_model": "gpt-6.1-terra", "reasoning_effort": "ultra"},
                },
            )
            assert len(remote.calls_of("harness_probe")) == 1
        with db_conn.cursor() as cur:
            cur.execute("SELECT birth_config FROM agents_meta WHERE id = %s", (first,))
            first_row = cur.fetchone()
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (second,))
            second_row = cur.fetchone()
        assert first_row is not None
        assert first_row[0]["harness_model_selection"] == 1
        assert first_row[0]["llm_model"] == "gpt-6.1-terra"
        assert first_row[0]["reasoning_effort"] == "low"
        assert second_row is not None
        assert second_row[0]["reasoning_effort"] == "ultra"

    def test_unprobed_model_or_effort_is_rejected_without_creating_an_agent(
        self, db_conn: psycopg.Connection
    ) -> None:
        before = _agent_count(db_conn)
        with gateway_test_client() as client:
            _seed_harnesses(client)
            bad_model = spawn_via_http(
                client,
                {"spawner": "user", "harness": "codex", "config": {"llm_model": "gpt-6.1-sol"}},
            )
            bad_effort = spawn_via_http(
                client,
                {
                    "spawner": "user",
                    "harness": "codex",
                    "config": {"llm_model": "gpt-6.1-terra", "reasoning_effort": "xhigh"},
                },
            )
        assert bad_model.status_code == 400
        assert bad_model.json()["reason"] == "invalid_model_config"
        assert bad_effort.status_code == 400
        assert bad_effort.json()["reason"] == "invalid_model_config"
        assert _agent_count(db_conn) == before

    def test_unavailable_probed_harness_is_rejected_without_creating_an_agent(
        self, db_conn: psycopg.Connection
    ) -> None:
        before = _agent_count(db_conn)
        with gateway_test_client() as client:
            _seed_harnesses(client)
            response = spawn_via_http(client, {"spawner": "user", "harness": "kimi_code"})
        assert response.status_code == 400
        assert response.json()["reason"] == "harness_not_launchable"
        assert _agent_count(db_conn) == before

    def test_spawn_without_a_harness_runs_the_builtin_runtime(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent = spawn_via_http(client, {"spawner": "user"}).json()["id"]
        assert _harness(db_conn, agent) == "meiva"

    def test_unknown_harness_is_rejected_without_creating_an_agent(
        self, db_conn: psycopg.Connection
    ) -> None:
        before = _agent_count(db_conn)
        with gateway_test_client() as client:
            response = spawn_via_http(client, {"spawner": "user", "harness": "claude-code"})
        assert response.status_code == 422
        assert _agent_count(db_conn) == before

    def test_fork_inherits_the_source_harness_through_http(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            source = _spawn(client, {"harness": "meiva"})
            _checkpoint(db_conn, source)
            fork = _spawn(client, {"fork_from": source})
        assert _harness(db_conn, source) == "meiva"
        assert _harness(db_conn, fork) == "meiva"

    def test_fork_of_an_external_harness_is_rejected_through_http(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            _seed_harnesses(client)
            source = _spawn(
                client,
                {
                    "harness": "antigravity",
                    "config": {"llm_model": "gemini-4-argon", "reasoning_effort": "high"},
                },
            )
            _checkpoint(db_conn, source)
            before = _agent_count(db_conn)
            response = spawn_via_http(client, {"spawner": "user", "fork_from": source})
        assert response.status_code == 400
        body = response.json()
        assert body["reason"] == "fork_harness_mismatch"
        assert "only supported on the native 'meiva' harness" in body["detail"]
        assert _agent_count(db_conn) == before

    def test_fork_naming_another_harness_is_rejected(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            source = spawn_via_http(client, {"spawner": "user"}).json()["id"]
            _checkpoint(db_conn, source)
            response = spawn_via_http(
                client,
                {"spawner": "user", "fork_from": source, "harness": "claude_code"},
            )
        assert response.status_code == 400
        body = response.json()
        assert body["reason"] == "fork_harness_mismatch"
        assert "immutable" in body["detail"]

    def test_fork_naming_its_own_harness_is_not_refused(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            source = _spawn(client, {"harness": "meiva"})
            _checkpoint(db_conn, source)
            fork = _spawn(client, {"fork_from": source, "harness": "meiva"})
        assert _harness(db_conn, fork) == "meiva"
