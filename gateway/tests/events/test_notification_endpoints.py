from __future__ import annotations

import psycopg

from base.events.live.bus import EventBus
from gateway.tests.auth.gateway_config import gateway_test_client


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
# The switch list is the two kinds of push the product has, both on by default: every transport
# honors this view, so a third category that ships on fails here.
# meiva:link base/events/notifications/README.md
def test_the_switch_list_is_the_question_and_the_unread_push_both_on(
    db_conn: psycopg.Connection,
) -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/notifications/categories")
    assert resp.status_code == 200
    categories = {item["id"]: item["enabled"] for item in resp.json()["categories"]}
    assert categories == {"core.question": True, "core.unread": True}


def test_a_plugin_can_no_longer_post_its_own_notification(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/agents/{agent_id}/notifications",
            json={"category": "core.unread", "subject": "hello"},
        )
    assert resp.status_code in {404, 405}


def _insert_delivery(conn: psycopg.Connection, agent_id: int, subject: str) -> int:
    row = conn.execute(
        "INSERT INTO notification_events (category, action, tag, subject, importance, agent_id) "
        "VALUES ('core.unread', 'alert', %s, %s, 'normal', %s) RETURNING id",
        (f"unread:{agent_id}", subject, agent_id),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


# meiva:note
# The recovery read: a client that was offline asks for what it missed, and gets
# the newest deliveries after its cursor plus the newest id so a first run can set
# a cursor without replaying history. Each row carries its action and tag, which is
# what lets the client fold the gap down to one state per notification.
# meiva:link base/events/notifications/README.md
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
    assert body["notifications"][0]["action"] == "alert"
    assert body["notifications"][0]["tag"] == f"unread:{agent_id}"
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
