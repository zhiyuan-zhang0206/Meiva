from __future__ import annotations

import json
from uuid import uuid4

import psycopg
import pytest

from base.api_contracts.agent_contract import Harness, HarnessBilling
from base.api_contracts.idempotency import keyed_headers
from base.lm.catalog import ModelCatalog
from gateway.schemas.models import HarnessAvailability, HarnessModelOption
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client, spawn_via_http
from gateway.tests.test_agents_endpoints import inbound_rows, terminate_hosted
from gateway.tests.test_agents_endpoints import withdrawn_model as withdrawn_model

pytestmark = pytest.mark.usefixtures("gateway_local_runner")


def _seed_restart_harnesses(client: GatewayTestClient) -> None:
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
                harness=Harness.CLAUDE_CODE,
                status="available",
                reason="signed in",
                launchable=True,
                billing=HarnessBilling.SUBSCRIPTION,
                models=[
                    HarnessModelOption(
                        id="claude-opus-4-6",
                        reasoning_effort_options=["low", "medium", "high"],
                        reasoning_effort_default="high",
                    )
                ],
            ),
        ],
    )


class TestSystemNote:
    def test_system_note_inserts_system_note_inbound(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "Context was refreshed.", "note_tag": "context"},
                headers=keyed_headers(str(uuid4())),
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "idling"
        assert body["inbound_id"] is not None
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, kind, source, payload FROM inbound_messages WHERE agent_id = %s",
                (agent_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        content, kind, source, payload = rows[0]
        assert kind == "system_note"
        assert source == "system"
        assert content == "Context was refreshed."
        assert payload == {"note_tag": "context", "delivery_resurrect": True}
        assert all(r[1] != "resurrect" for r in rows)

    def test_system_note_to_terminated_agent_resurrects_when_requested(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            terminate_hosted(db_conn, agent_id)
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "Context was refreshed.", "note_tag": "context"},
                headers=keyed_headers(str(uuid4())),
            )
        assert resp.status_code == 201
        rows = inbound_rows(db_conn, agent_id)
        assert ("", "resurrect", "system") in rows
        assert any(kind == "system_note" for _, kind, _ in rows)

    def test_system_note_to_terminated_agent_no_resurrect_when_denied(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            terminate_hosted(db_conn, agent_id)
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={
                    "content": "Context was refreshed.",
                    "note_tag": "context",
                    "resurrect": False,
                },
                headers=keyed_headers(str(uuid4())),
            )
        assert resp.status_code == 201
        rows = inbound_rows(db_conn, agent_id)
        assert rows == [("Context was refreshed.", "system_note", "system")]
        assert all(r[1] != "resurrect" for r in rows)

    def test_system_note_unknown_tag_rejected_422(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "note_tag": "not_a_tag"},
                headers=keyed_headers(str(uuid4())),
            )
        assert resp.status_code == 422

    def test_system_note_requires_explicit_tag(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "System notice"},
                headers=keyed_headers(str(uuid4())),
            )
        assert resp.status_code == 422
        assert resp.json()["errors"][0]["loc"] == ["body", "note_tag"]

    def test_system_note_illegal_source_rejected_422(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "source": "ui:web", "note_tag": "context"},
                headers=keyed_headers(str(uuid4())),
            )
        assert resp.status_code == 422


class TestRestart:
    @pytest.mark.parametrize(
        ("initial", "overlay"),
        [
            (
                {"llm_model": "deepseek-flash", "reasoning_effort": "max"},
                {"reasoning_effort": "low"},
            ),
            (
                {"llm_model": "gpt-5.6-sol", "reasoning_effort": "low"},
                {"llm_model": "deepseek-flash"},
            ),
        ],
    )
    def test_incompatible_merged_model_effort_is_rejected_without_writes(
        self, db_conn: psycopg.Connection, initial: dict[str, str], overlay: dict[str, str]
    ) -> None:
        with gateway_test_client() as client:
            spawned = spawn_via_http(client, {"config": initial})
            assert spawned.status_code == 201, spawned.text
            agent_id = spawned.json()["id"]
            response = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"message": "restarting", "config_overlay": overlay},
            )
        assert response.status_code == 400, response.text
        assert "unsupported reasoning effort" in response.json()["detail"]
        assert db_conn.execute(
            "SELECT config_overlay FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone() == (initial,)
        assert db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart'",
            (agent_id,),
        ).fetchone() == (0,)

    def test_restart_inserts_inbound_and_returns_enqueued(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(f"/api/agents/{agent_id}/restart", json={"message": "restarting"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued"}
        assert inbound_rows(db_conn, agent_id) == [("restarting", "restart", "user")]

    def test_restart_persists_message_and_rejects_blank_input(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            bad = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"message": "   "},
            )
            assert bad.status_code == 422
            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"message": "  Zhiyuan reloading skill  "},
            )
        assert resp.status_code == 200
        assert inbound_rows(db_conn, agent_id) == [("Zhiyuan reloading skill", "restart", "user")]

    def test_restart_merges_config_overlay_and_records_it_in_the_inbound(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
                    (json.dumps({"reasoning_effort": "low"}), agent_id),
                )
            db_conn.commit()
            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"message": "restarting", "config_overlay": {"llm_model": "gpt-5.6-sol"}},
            )

        assert resp.status_code == 200
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
            assert cur.fetchone() == ({"reasoning_effort": "low", "llm_model": "gpt-5.6-sol"},)
            cur.execute(
                "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'restart'",
                (agent_id,),
            )
            assert cur.fetchone() == ({"config_overlay": {"llm_model": "gpt-5.6-sol"}},)

    def test_restart_settles_withdrawn_model_before_storing(
        self,
        db_conn: psycopg.Connection,
        withdrawn_model: tuple[str, ModelCatalog],
    ) -> None:
        model, catalog = withdrawn_model
        with gateway_test_client(catalog=catalog) as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"message": "restarting", "config_overlay": {"llm_model": model}},
            )
        assert resp.status_code == 200, resp.text
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
            assert cur.fetchone() == ({"llm_model": "deepseek-flash"},)
            cur.execute(
                "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'restart'",
                (agent_id,),
            )
            assert cur.fetchone() == ({"config_overlay": {"llm_model": "deepseek-flash"}},)

    @pytest.mark.parametrize("config_overlay", [None, {}])
    def test_restart_empty_config_overlay_keeps_legacy_restart_shape(
        self, db_conn: psycopg.Connection, config_overlay: dict[str, object] | None
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
                    (json.dumps({"reasoning_effort": "low"}), agent_id),
                )
            db_conn.commit()
            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={"message": "restarting", "config_overlay": config_overlay},
            )

        assert resp.status_code == 200
        with db_conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,))
            assert cur.fetchone() == ({"reasoning_effort": "low"},)
            cur.execute(
                "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'restart'",
                (agent_id,),
            )
            assert cur.fetchone() == (None,)

    def test_restart_invalid_config_overlay_is_rejected_before_enqueue(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "restarting",
                    "config_overlay": {"definitely_not_a_config_field": "x"},
                },
            )

        assert resp.status_code == 422
        assert inbound_rows(db_conn, agent_id) == []

    def test_restart_already_terminated_is_noop(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,)
                )
            db_conn.commit()
            resp = client.post(f"/api/agents/{agent_id}/restart", json={"message": "restarting"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "already_terminated"}
        assert inbound_rows(db_conn, agent_id) == []

    def test_restart_nonexistent_404(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            resp = client.post("/api/agents/9999/restart", json={"message": "restarting"})
        assert resp.status_code == 404

    def test_harness_switch_meiva_to_codex(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            _seed_restart_harnesses(client)
            agent_id = spawn_via_http(client, {}).json()["id"]

            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "switching to codex",
                    "config_overlay": {"harness": "codex", "llm_model": "gpt-6.1-sol"},
                },
            )
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"status": "enqueued"}

            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT harness, birth_config, config_overlay, harness_configured_at "
                    "FROM agents_meta WHERE id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "codex"
                assert row[1].get("harness_model_selection") == 1
                assert row[2].get("llm_model") == "gpt-6.1-sol"
                assert row[2].get("reasoning_effort") == "medium"
                assert "harness" not in row[2]
                assert row[3] is not None

    def test_harness_switch_codex_to_meiva(self, db_conn: psycopg.Connection) -> None:
        from base import paths
        from base.lm.harness.registry import FileSessionRegistry, SessionRecord

        with gateway_test_client() as client:
            _seed_restart_harnesses(client)
            agent_id = spawn_via_http(client, {}).json()["id"]

            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "switching to codex",
                    "config_overlay": {"harness": "codex", "llm_model": "gpt-6.1-sol"},
                },
            )
            assert resp.status_code == 200

            sessions_dir = paths.harness_sessions_dir(home=client.app.state.gateway_process.home)
            registry = FileSessionRegistry(sessions_dir)
            registry.save(
                SessionRecord(
                    agent_id=agent_id,
                    session_id="test",
                    cwd="/w",
                    model="gpt-6.1-sol",
                    created_at="2026-11-01T00:00:00Z",
                    updated_at="2026-11-01T00:00:00Z",
                    harness="codex",
                    generation=0,
                )
            )

            resp2 = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "switching back to meiva",
                    "config_overlay": {"harness": "meiva", "llm_model": "deepseek-flash"},
                },
            )
            assert resp2.status_code == 200, resp2.text
            assert resp2.json() == {"status": "enqueued"}
            assert registry.load(agent_id, harness="codex") is not None

            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT harness, config_overlay FROM agents_meta WHERE id = %s",
                    (agent_id,),
                )
                row2 = cur.fetchone()
                assert row2 is not None
                assert row2[0] == "meiva"
                assert row2[1].get("llm_model") == "deepseek-flash"

    # meiva:note
    # A terminated agent keeps its row, so a switch has nothing to settle first: the preconditions are
    # about a live turn and an open lease, and a dead agent has neither.
    def test_a_terminated_agent_switches_harness(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            _seed_restart_harnesses(client)
            agent_id = spawn_via_http(client, {}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,)
                )
            db_conn.commit()

            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "switch while terminated",
                    "config_overlay": {"harness": "codex", "llm_model": "gpt-6.1-sol"},
                },
            )
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"status": "enqueued"}

            with db_conn.cursor() as cur:
                cur.execute("SELECT harness FROM agents_meta WHERE id = %s", (agent_id,))
                row = cur.fetchone()
            assert row is not None and row[0] == "codex"

    @pytest.mark.parametrize(
        ("old_harness", "old_model"),
        [("meiva", None), ("codex", "gpt-6.1-sol"), ("claude_code", "claude-opus-4-6")],
    )
    def test_external_restart_message_is_a_lifecycle_note(
        self, db_conn: psycopg.Connection, old_harness: str, old_model: str | None
    ) -> None:
        with gateway_test_client() as client:
            _seed_restart_harnesses(client)
            agent_id = spawn_via_http(client, {}).json()["id"]
            if old_model is not None:
                first = client.post(
                    f"/api/agents/{agent_id}/restart",
                    json={
                        "message": "preparing the starting harness",
                        "config_overlay": {"harness": old_harness, "llm_model": old_model},
                    },
                )
                assert first.status_code == 200, first.text

            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "continue checking",
                    "config_overlay": {"harness": "codex", "llm_model": "gpt-6.1-sol"},
                },
            )
            assert resp.status_code == 200, resp.text

        expected = "continue checking"
        if old_harness != "codex":
            expected = f"[Harness switched from '{old_harness}' to 'codex']\n" + expected
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, kind, source, payload FROM inbound_messages "
                "WHERE agent_id = %s AND content LIKE %s",
                (agent_id, "%continue checking"),
            )
            assert cur.fetchall() == [
                (expected, "system_note", "user", {"note_tag": "lifecycle_restart"})
            ]

    def test_harness_switch_preconditions(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]

            with db_conn.cursor() as cur:
                cur.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (agent_id,))
            db_conn.commit()

            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "switch while running",
                    "config_overlay": {"harness": "codex", "llm_model": "gpt-6.1-sol"},
                },
            )
            assert resp.status_code == 409
            assert "Cannot restart agent in status 'running'" in resp.json()["detail"]

            with db_conn.cursor() as cur:
                cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (agent_id,))
            db_conn.commit()

            resp_blank = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "   ",
                    "config_overlay": {"harness": "codex", "llm_model": "gpt-6.1-sol"},
                },
            )
            assert resp_blank.status_code == 422

            resp_bad_harness = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "bad harness",
                    "config_overlay": {"harness": "not-a-valid-harness"},
                },
            )
            assert resp_bad_harness.status_code == 422

            _seed_restart_harnesses(client)
            resp_bad_model = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "bad model",
                    "config_overlay": {"harness": "claude_code", "llm_model": "gpt-6.1-sol"},
                },
            )
            assert resp_bad_model.status_code == 400
            assert "not supported by harness 'claude_code'" in resp_bad_model.json()["detail"]

    @pytest.mark.parametrize(
        ("old_harness", "old_model", "restart_overlay"),
        [
            ("codex", "gpt-6.1-sol", None),
            ("codex", "gpt-6.1-sol", {"harness": "codex", "llm_model": "gpt-6.1-sol"}),
            ("meiva", None, {"harness": "codex", "llm_model": "gpt-6.1-sol"}),
            ("codex", "gpt-6.1-sol", {"harness": "meiva", "llm_model": "deepseek-flash"}),
        ],
    )
    def test_external_harness_restart_preserves_shell_sessions(
        self,
        db_conn: psycopg.Connection,
        old_harness: str,
        old_model: str | None,
        restart_overlay: dict[str, object] | None,
    ) -> None:
        with gateway_test_client() as client:
            _seed_restart_harnesses(client)
            agent_id = spawn_via_http(client, {}).json()["id"]
            if old_model is not None:
                switched = client.post(
                    f"/api/agents/{agent_id}/restart",
                    json={
                        "message": "prepare external harness",
                        "config_overlay": {"harness": old_harness, "llm_model": old_model},
                    },
                )
                assert switched.status_code == 200, switched.text
                with db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE agents_meta SET status = 'idling' WHERE id = %s",
                        (agent_id,),
                    )
                db_conn.commit()

            body: dict[str, object] = {"message": "restart keeping shells"}
            if restart_overlay is not None:
                body["config_overlay"] = restart_overlay
            resp = client.post(f"/api/agents/{agent_id}/restart", json=body)
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"status": "enqueued"}

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM inbound_messages WHERE agent_id = %s AND kind = 'terminate'",
                (agent_id,),
            )
            payloads = [row[0] for row in cur.fetchall()]
        assert payloads
        assert all(
            not isinstance(payload, dict) or "kill_all_shell_sessions" not in payload
            for payload in payloads
        )
