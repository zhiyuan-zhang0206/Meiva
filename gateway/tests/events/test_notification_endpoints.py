from __future__ import annotations

import json
from pathlib import Path

import psycopg

from base.api_contracts.idempotency import keyed_headers
from base.events.live.bus import EventBus
from gateway.extensions.notification_categories import categories_from_manifests
from tests.fixtures.gateway_config import gateway_test_client


def _agent(conn: psycopg.Connection) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    conn.execute(
        "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'idling')",
        (agent_id,),
    )
    conn.commit()
    return agent_id


# meiva:note
# The default configuration enables the one request the user has to answer. A
# category the core or an installed plugin adds that ships on fails here, because
# every transport honors this merged view.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_only_the_request_that_needs_a_reply_is_enabled(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/notifications/categories")
    assert resp.status_code == 200
    categories = {item["id"]: item for item in resp.json()["categories"]}
    core = {item_id for item_id in categories if item_id.startswith("core.")}
    assert core == {"core.needs_input"}
    assert {item_id for item_id, item in categories.items() if item["enabled"]} == {
        "core.needs_input"
    }


def test_an_undeclared_category_is_rejected(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{agent_id}/notifications",
            json={"category": "nobody.nothing", "subject": "hello"},
        )
    assert resp.status_code == 422


def test_a_delivery_is_recorded_and_deduplicated(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    payload = {
        "category": "core.needs_input",
        "subject": "Agent is waiting on your answer",
        "dedupe_key": "notice:test",
    }
    with gateway_test_client() as client:
        first = client.post(f"/api/agents/{agent_id}/notifications", json=payload)
        second = client.post(f"/api/agents/{agent_id}/notifications", json=payload)
    assert first.status_code == 201
    assert first.json()["delivered"] is True
    assert second.json() == {"delivered": False, "id": None}
    rows = db_conn.execute(
        "SELECT category, subject FROM notification_events WHERE agent_id = %s", (agent_id,)
    ).fetchall()
    assert rows == [("core.needs_input", "Agent is waiting on your answer")]


def test_an_empty_subject_is_rejected(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{agent_id}/notifications",
            json={"category": "core.needs_input", "subject": "   "},
        )
    assert resp.status_code == 422


def _deliveries(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, object]]:
    rows = conn.execute(
        "SELECT category, subject, announced_at FROM notification_events "
        "WHERE agent_id = %s ORDER BY id",
        (agent_id,),
    ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


# meiva:note
# A notice that needs an answer is staged in the notice's own transaction and
# announced after it commits, so the row exists and is marked delivered.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_a_response_notice_is_staged_and_announced(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    agent_id = _agent(db_conn)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{agent_id}/notices",
            json={"title": "Need a decision", "require_response": True},
            headers=keyed_headers("staged-announced"),
        )
    assert resp.status_code == 201
    deliveries = _deliveries(db_conn, agent_id)
    assert [(category, subject) for category, subject, _ in deliveries] == [
        ("core.needs_input", "Need a decision")
    ]
    assert deliveries[0][2] is not None


# meiva:note
# The replay branch repairs the delivery instead of skipping it: a post whose
# announce was lost (the client retried because the response never arrived) is
# announced by the retry, and the row is not duplicated.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_a_replayed_post_repairs_a_lost_announce(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    agent_id = _agent(db_conn)
    headers = keyed_headers("replay-repair")
    payload = {"title": "Need a decision", "require_response": True}
    with gateway_test_client() as client:
        first = client.post(f"/api/agents/{agent_id}/notices", json=payload, headers=headers)
        assert first.status_code == 201
        # meiva:note
        # The announce never happened: the process died right after the commit.
        # meiva:link base/events/notifications/docs/notifications.meiva.okf.md
        db_conn.execute(
            "UPDATE notification_events SET announced_at = NULL WHERE agent_id = %s",
            (agent_id,),
        )
        db_conn.commit()
        replay = client.post(f"/api/agents/{agent_id}/notices", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == first.json()
    deliveries = _deliveries(db_conn, agent_id)
    assert len(deliveries) == 1
    assert deliveries[0][2] is not None


def _insert_delivery(conn: psycopg.Connection, agent_id: int, subject: str) -> int:
    row = conn.execute(
        "INSERT INTO notification_events (category, subject, importance, agent_id) "
        "VALUES ('core.needs_input', %s, 'high', %s) RETURNING id",
        (subject, agent_id),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


# meiva:note
# The recovery read: a client that was offline asks for what it missed, and gets
# the newest deliveries after its cursor plus the newest id so a first run can set
# a cursor without replaying history.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_the_pull_path_returns_deliveries_after_a_cursor(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    agent_id = _agent(db_conn)
    first = _insert_delivery(db_conn, agent_id, "first")
    second = _insert_delivery(db_conn, agent_id, "second")
    with gateway_test_client() as client:
        caught_up = client.get(f"/api/notifications?after_id={first}")
        from_scratch = client.get("/api/notifications?after_id=0&limit=100")
    assert caught_up.status_code == 200
    body = caught_up.json()
    assert [item["id"] for item in body["notifications"]] == [second]
    assert body["notifications"][0]["subject"] == "second"
    assert body["latest_id"] == second
    ids = [item["id"] for item in from_scratch.json()["notifications"]]
    assert first in ids and second in ids


def test_the_pull_path_keeps_the_newest_rows_of_a_long_gap(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    agent_id = _agent(db_conn)
    ids = [_insert_delivery(db_conn, agent_id, f"n{index}") for index in range(3)]
    with gateway_test_client() as client:
        resp = client.get("/api/notifications?after_id=0&limit=2")
    assert [item["id"] for item in resp.json()["notifications"]] == ids[1:]


# meiva:note
# A plugin's declared category joins the merged switch list — switched off, with
# its dedupe window intact — so the plugin path stays exercised after the core
# catalog dropped to one category. The manifest is a real one on disk, parsed by
# the same loader the gateway uses.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_a_plugin_category_joins_the_switch_list_switched_off(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "sample"
    plugin_dir.mkdir()
    (plugin_dir / "meiva-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": "sample",
                "version": "1.0.0",
                "engines": {"meiva": ">=0.0.1"},
                "contributions": {
                    "notifications": [
                        {
                            "id": "sample.question",
                            "label": "A question",
                            "description": "The sample plugin needs an answer.",
                            "importance": "high",
                            "defaultEnabled": False,
                            "dedupeWindowSeconds": 60,
                        }
                    ]
                },
            }
        )
    )

    catalog = categories_from_manifests({"sample": plugin_dir}, {"sample"})

    assert set(catalog) == {"core.needs_input", "sample.question"}
    assert catalog["sample.question"].default_enabled is False
    assert catalog["sample.question"].dedupe_window_seconds == 60
