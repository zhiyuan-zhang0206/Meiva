from __future__ import annotations

import json
import threading
import time
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.api_contracts.idempotency import keyed_headers
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from gateway.tests.auth.gateway_config import gateway_test_client, spawn_via_http
from gateway.tests.test_agents_endpoints import inbound_rows, returned_id, terminate_hosted
from gateway.tests.test_agents_endpoints import withdrawn_model as withdrawn_model

pytestmark = pytest.mark.usefixtures("gateway_local_runner")


def _wait_until_task_is_locked(db_conn: psycopg.Connection, task_id: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with db_conn.transaction():
                db_conn.execute(
                    "SELECT id FROM agent_tasks WHERE id = %s FOR UPDATE NOWAIT", (task_id,)
                )
        except psycopg.errors.LockNotAvailable:
            return
        time.sleep(0.02)
    raise AssertionError("the note never locked the task")


class TestSystemNote:
    def test_system_note_inserts_system_note_inbound(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": 'Task #1 "t" is now assigned to you.'},
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
        assert "assigned to you" in content
        assert payload == {"note_tag": "task", "delivery_resurrect": True}
        assert all(r[1] != "resurrect" for r in rows)

    def test_system_note_to_terminated_agent_resurrects_when_requested(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            terminate_hosted(db_conn, agent_id)
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": 'Task #1 "t" is now assigned to you.'},
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
                json={"content": 'Task #1 "t" was updated.', "resurrect": False},
                headers=keyed_headers(str(uuid4())),
            )
        assert resp.status_code == 201
        rows = inbound_rows(db_conn, agent_id)
        assert rows == [('Task #1 "t" was updated.', "system_note", "system")]
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

    def test_system_note_task_id_requires_task_tag(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "note_tag": "heartbeat", "task_id": 42},
                headers=keyed_headers(str(uuid4())),
            )
        assert resp.status_code == 422
        assert "task_id requires note_tag='task'" in str(resp.json())

    def test_system_note_task_id_must_name_an_existing_task(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "task_id": 999_999},
                headers=keyed_headers(str(uuid4())),
            )
        assert resp.status_code == 422
        assert "task_id 999999 does not exist" in str(resp.json())

    def test_system_note_task_id_must_belong_to_the_recipient(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            owner_id = spawn_via_http(client, {}).json()["id"]
            recipient_id = spawn_via_http(client, {}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_tasks (title, description, created_by, owner) "
                    "VALUES ('owned task', 'd', 'user', %s) RETURNING id",
                    (owner_id,),
                )
                row = cur.fetchone()
            assert row is not None
            db_conn.commit()
            resp = client.post(
                f"/api/agents/{recipient_id}/system-note",
                json={"content": "x", "task_id": row[0]},
                headers=keyed_headers(str(uuid4())),
            )
        assert resp.status_code == 422
        assert "is not owned by agent" in str(resp.json())

    def test_system_note_task_ownership_stays_locked_through_enqueue(
        self,
        db_conn: psycopg.Connection,
        db_url: str,
        database: Database,
        event_bus: EventBus,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        from base.db import connect, pool
        from gateway.agents import system_note

        reassign_started, reassign_finished = threading.Event(), threading.Event()
        errors: list[Exception] = []

        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
            owner_id = returned_id(cur)
            cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
            new_owner_id = returned_id(cur)
            cur.execute(
                "INSERT INTO agent_tasks (title, description, created_by, owner) "
                "VALUES ('locked task', 'd', 'user', %s) RETURNING id",
                (owner_id,),
            )
            task_id = returned_id(cur)
        db_conn.commit()

        def enqueue(note_pool: ConnectionPool) -> None:
            try:
                system_note.system_note_blocking(
                    database,
                    event_bus,
                    note_pool,
                    owner_id,
                    "x",
                    "system",
                    "task",
                    task_id,
                    client_message_id="task-owner-fence",
                )
            except Exception as exc:
                errors.append(exc)

        def reassign() -> None:
            try:
                with connect(gate=database_gate) as conn, conn.cursor() as cur:
                    reassign_started.set()
                    cur.execute(
                        "UPDATE agent_tasks SET owner = %s WHERE id = %s",
                        (new_owner_id, task_id),
                    )
            except Exception as exc:
                errors.append(exc)
            finally:
                reassign_finished.set()

        # meiva:note
        # A session holding the inbox table against inserts parks the note at its insert, after
        # it has locked the task row; the reassignment must wait for the note's transaction.
        with (
            psycopg.connect(db_url) as holder,
            pool(max_size=1, gate=database_gate) as note_pool,
        ):
            holder.execute("LOCK TABLE inbound_messages IN SHARE MODE")
            enqueue_thread, reassign_thread = (
                threading.Thread(target=enqueue, args=(note_pool,), daemon=True),
                threading.Thread(target=reassign, daemon=True),
            )
            enqueue_thread.start()
            try:
                _wait_until_task_is_locked(db_conn, task_id)
                reassign_thread.start()
                assert reassign_started.wait(timeout=2)
                assert not reassign_finished.wait(timeout=0.2), (
                    "task reassignment committed while the task note was being enqueued"
                )
            finally:
                holder.rollback()
                enqueue_thread.join(timeout=5)
                if reassign_thread.ident is not None:
                    reassign_thread.join(timeout=5)

        assert not enqueue_thread.is_alive()
        assert not reassign_thread.is_alive()
        assert errors == []

    def test_system_note_illegal_source_rejected_422(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            resp = client.post(
                f"/api/agents/{agent_id}/system-note",
                json={"content": "x", "source": "ui:web"},
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
        from base.agents.history.harness_messages import append_harness_timeline_items
        from base.agents.history.timeline_item import TimelineItem

        with gateway_test_client() as client:
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
                    "SELECT harness, birth_config, config_overlay FROM agents_meta WHERE id = %s",
                    (agent_id,),
                )
                row = cur.fetchone()
                assert row is not None
                assert row[0] == "codex"
                assert row[1].get("harness_model_selection") == 1
                assert row[2].get("llm_model") == "gpt-6.1-sol"
                assert "harness" not in row[2]

            item = TimelineItem(
                item_id="1.0",
                kind="agent_chat",
                source="codex",
                payload="hello from codex",
                created_at="2026-11-01T12:00:00Z",
            )
            append_harness_timeline_items(db_conn, agent_id, "codex", [item])
            db_conn.commit()

            t_resp = client.get(f"/api/agents/{agent_id}/timeline")
            assert t_resp.status_code == 200
            items = t_resp.json()["items"]
            assert any(i.get("payload") == "hello from codex" for i in items)

    def test_harness_switch_codex_to_meiva(self, db_conn: psycopg.Connection) -> None:
        from base import paths
        from base.lm.harness.registry import FileSessionRegistry

        with gateway_test_client() as client:
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
            registry.path(agent_id).parent.mkdir(parents=True, exist_ok=True)
            registry.path(agent_id).write_text(
                '{"session_id": "test", "harness": "codex", "agent_id": '
                + str(agent_id)
                + ', "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-01T00:00:00Z"}'
            )
            assert registry.path(agent_id).exists()

            resp2 = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "switching back to meiva",
                    "config_overlay": {"harness": "meiva", "llm_model": "deepseek-flash"},
                },
            )
            assert resp2.status_code == 200, resp2.text
            assert resp2.json() == {"status": "enqueued"}
            assert not registry.path(agent_id).exists()

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

    # meiva:note
    # An open or unsettled impersonation lease owns the agent's identity, so a switch must not move it
    # out from under the impersonator: the refusal comes before any status check.
    def test_a_switch_under_an_open_lease_is_refused(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_impersonations "
                    "(id, agent_id, source, machine, status, ttl_seconds, expires_at, activated_at) "
                    "VALUES (%s, %s, 'external_agent:codex', 'test-machine', 'active', 3600, "
                    "clock_timestamp() + interval '1 hour', clock_timestamp())",
                    (str(uuid4()), agent_id),
                )
            db_conn.commit()

            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "switch under a lease",
                    "config_overlay": {"harness": "codex"},
                },
            )
            assert resp.status_code == 409
            assert resp.json()["detail"] == "Agent has an active impersonation lease"

            with db_conn.cursor() as cur:
                cur.execute("SELECT harness FROM agents_meta WHERE id = %s", (agent_id,))
                row = cur.fetchone()
            assert row is not None and row[0] == "meiva"

    # meiva:note
    # A switch tells the agent what happened: the wake prompt opens with
    # `[Harness switched from '<old>' to '<new>']`, so the first thing the new harness reads names the
    # transition (ops/lifecycle/__init__.py builds it when the two harnesses differ).
    def test_a_switch_names_the_transition_in_the_wake_prompt(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            agent_id = spawn_via_http(client, {}).json()["id"]

            resp = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "switching to codex",
                    "config_overlay": {"harness": "codex", "llm_model": "gpt-6.1-sol"},
                },
            )
            assert resp.status_code == 200, resp.text

        rows = inbound_rows(db_conn, agent_id)
        [content] = [
            text
            for text, _kind, _source in rows
            if text.startswith("[Harness switched from 'meiva' to 'codex']")
        ]
        assert content.endswith("switching to codex")

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
                    "config_overlay": {"harness": "codex"},
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
                    "config_overlay": {"harness": "codex"},
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

            resp_bad_model = client.post(
                f"/api/agents/{agent_id}/restart",
                json={
                    "message": "bad model",
                    "config_overlay": {"harness": "kimi_code", "llm_model": "gpt-6.1-sol"},
                },
            )
            assert resp_bad_model.status_code == 400
            assert "not supported by harness 'kimi_code'" in resp_bad_model.json()["detail"]
