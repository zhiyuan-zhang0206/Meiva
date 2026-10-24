# meiva:note
# The label of an agent over HTTP: a user's PATCH writes it (and marks it user-set), an agent's
# spawn leaves it for the labeler, and a changed label is announced on the events channel.

from __future__ import annotations

import psycopg
import pytest

from base.agents.labels import publish_label_updated
from base.config import settings
from base.db import create_agent
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import recorded_events
from gateway.tests.auth.gateway_config import gateway_test_client, spawn_via_http
from gateway.tests.support.db_faults.insert_fault import failing_insert


def _label_of(conn: psycopg.Connection, agent_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        assert row is not None
        return row[0]


def _label_user_set(conn: psycopg.Connection, agent_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT label_user_set FROM agents WHERE id = %s", (agent_id,))
        row = cur.fetchone()
        assert row is not None
        return row[0]


def _label_audit(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, object]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, attributes FROM audit_events "
            "WHERE agent_id=%s AND event_name='label_change' ORDER BY id",
            (agent_id,),
        )
        rows = cur.fetchall()
    conn.commit()
    return [(row[0], row[1]) for row in rows]


class TestPatchThread:
    def test_patch_writes_label_and_flips_user_set(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        assert _label_user_set(db_conn, tid) is False
        with gateway_test_client() as client:
            resp = client.patch(
                f"/api/agents/{tid}", json={"label": "\u6211\u81ea\u5df1\u8d77\u7684\u540d"}
            )
        assert resp.status_code == 204
        assert _label_of(db_conn, tid) == "\u6211\u81ea\u5df1\u8d77\u7684\u540d"
        assert _label_user_set(db_conn, tid) is True

    def test_patch_reset_also_flips_user_set(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents SET label='\u4e34\u65f6' WHERE id=%s", (tid,))
        db_conn.commit()
        with gateway_test_client() as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": ""})
        assert resp.status_code == 204
        assert _label_of(db_conn, tid) is None
        assert _label_user_set(db_conn, tid) is True

    def test_patch_empty_resets_to_null(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents SET label='\u4e34\u65f6' WHERE id=%s", (tid,))
        db_conn.commit()
        with gateway_test_client() as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": ""})
        assert resp.status_code == 204
        assert _label_of(db_conn, tid) is None

    def test_patch_records_the_change_as_a_user_audit_fact(
        self, db_conn: psycopg.Connection
    ) -> None:
        tid = create_agent(db_conn)
        with gateway_test_client() as client:
            assert client.patch(f"/api/agents/{tid}", json={"label": "ops"}).status_code == 204
        assert _label_audit(db_conn, tid) == [("user", {"new_label": "ops"})]

    def test_patch_names_the_agent_as_the_source_when_it_sets_its_own_label(
        self, db_conn: psycopg.Connection
    ) -> None:
        tid = create_agent(db_conn)
        with gateway_test_client() as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": "lead", "source": "self"})
        assert resp.status_code == 204
        assert _label_audit(db_conn, tid) == [(f"agent:{tid}", {"new_label": "lead"})]

    # meiva:note
    # The audit insert fails inside the database (a trigger that raises after the row is
    # written), so the label update rolls back with it.
    def test_patch_whose_audit_fact_cannot_be_recorded_does_not_set_the_label(
        self, db_conn: psycopg.Connection
    ) -> None:
        tid = create_agent(db_conn)

        with (
            failing_insert(db_conn, "audit_events", message="audit write failed"),
            gateway_test_client(raise_server_exceptions=False) as client,
        ):
            resp = client.patch(f"/api/agents/{tid}", json={"label": "never set"})
        assert resp.status_code == 500
        assert _label_of(db_conn, tid) is None
        assert _label_audit(db_conn, tid) == []

    def test_patch_of_an_unknown_agent_is_404_and_records_nothing(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            resp = client.patch("/api/agents/2147483000", json={"label": "ghost"})
        assert resp.status_code == 404
        assert _label_audit(db_conn, 2147483000) == []

    def test_patch_strips_whitespace(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        with gateway_test_client() as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": "   x   "})
        assert resp.status_code == 204
        assert _label_of(db_conn, tid) == "x"

    def test_patch_only_whitespace_resets_to_null(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents SET label='something' WHERE id=%s", (tid,))
        db_conn.commit()
        with gateway_test_client() as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": "    "})
        assert resp.status_code == 204
        assert _label_of(db_conn, tid) is None

    def test_patch_404_when_thread_missing(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            resp = client.patch("/api/agents/9999", json={"label": "x"})
        assert resp.status_code == 404

    def test_patch_too_long_label_422(self, db_conn: psycopg.Connection) -> None:
        tid = create_agent(db_conn)
        long_label = "x" * 100
        with gateway_test_client() as client:
            resp = client.patch(f"/api/agents/{tid}", json={"label": long_label})
        assert resp.status_code == 422


@pytest.mark.usefixtures("gateway_local_runner")
class TestSpawnAgentSchedulesLabelGeneration:
    def test_spawn_with_prompt_leaves_label_null(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            resp = spawn_via_http(
                client, {"prompt": "\u8fc1\u79fb\u6570\u636e\u5e93 schema", "prompt_source": "user"}
            )
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        assert _label_of(db_conn, new_id) is None

    def test_spawn_without_prompt_leaves_label_null(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            resp = spawn_via_http(client, {})
        assert resp.status_code == 201
        new_id = resp.json()["id"]
        assert _label_of(db_conn, new_id) is None


class TestPublishLabelUpdated:
    @pytest.mark.asyncio
    async def test_publish_label_updated_uses_settings_channel(self, event_bus: EventBus) -> None:
        assert event_bus.channel == settings.data_plane.events_channel
        with recorded_events(event_bus) as events:
            await publish_label_updated(event_bus, 42, "\u77ed\u540d")
        assert [(e["role"], e["agent_id"], e["label"]) for e in events] == [
            ("label_updated", 42, "\u77ed\u540d")
        ]
