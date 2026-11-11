from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from base.agents.to_user.delivery.store import advance_seen, publish
from base.api_contracts.user_delivery import PublishMessage, UserDeliveryMode
from base.db import create_agent
from base.events.notifications.catalog import UNREAD_CATEGORY
from base.events.notifications.publish import stage_question_asked, stage_question_resolved
from base.events.notifications.store import SETTINGS_KEY

NOW = datetime(2026, 11, 11, 4, 0, tzinfo=UTC)


def _agent(conn: psycopg.Connection, label: str | None = "Builder") -> int:
    agent_id = create_agent(conn)
    conn.execute("UPDATE agents SET label = %s WHERE id = %s", (label, agent_id))
    conn.commit()
    return agent_id


def _say(conn: psycopg.Connection, agent_id: int, key: str, content: str | None) -> int:
    message = publish(
        conn,
        agent_id,
        key,
        PublishMessage(content=content, suggestions=[]),
        mode=UserDeliveryMode.SDK,
        created_at=NOW,
    )
    return message.id


def _pushes(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str, str | None]]:
    rows = conn.execute(
        "SELECT action, tag, subject, body FROM notification_events "
        "WHERE agent_id = %s ORDER BY id",
        (agent_id,),
    ).fetchall()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


# meiva:note
# The unread round as the cursor defines it: the first output after everything was read alerts, each
# later output only replaces the content of that one push, and reading the newest output retracts it.
# The next output after that is a new round and alerts again.
# meiva:link base/events/notifications/README.md
def test_one_unread_push_per_agent_follows_the_seen_cursor(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    tag = f"unread:{agent_id}"

    _say(db_conn, agent_id, "a", "first output")
    _say(db_conn, agent_id, "b", "second output")
    newest = _say(db_conn, agent_id, "c", "third output")
    assert _pushes(db_conn, agent_id) == [
        ("alert", tag, "Builder", "first output"),
        ("replace", tag, "Builder", "second output"),
        ("replace", tag, "Builder", "third output"),
    ]

    advance_seen(db_conn, agent_id, newest)
    assert _pushes(db_conn, agent_id)[-1] == ("retract", tag, "Builder", None)

    _say(db_conn, agent_id, "d", "fourth output")
    assert _pushes(db_conn, agent_id)[-1] == ("alert", tag, "Builder", "fourth output")


def test_a_cursor_move_that_leaves_newer_output_unread_keeps_the_push(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _agent(db_conn)
    first = _say(db_conn, agent_id, "a", "first")
    _say(db_conn, agent_id, "b", "second")

    advance_seen(db_conn, agent_id, first)

    assert [action for action, *_ in _pushes(db_conn, agent_id)] == ["alert", "replace"]


def test_the_title_is_the_agent_display_name_with_the_id_as_fallback(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _agent(db_conn, label=None)
    _say(db_conn, agent_id, "a", "hello")
    assert _pushes(db_conn, agent_id) == [("alert", f"unread:{agent_id}", f"#{agent_id}", "hello")]


def test_a_replayed_publication_stages_nothing_more(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    _say(db_conn, agent_id, "same", "hello")
    _say(db_conn, agent_id, "same", "hello")
    assert len(_pushes(db_conn, agent_id)) == 1


def test_declaring_no_update_stages_nothing(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    _say(db_conn, agent_id, "quiet", None)
    assert _pushes(db_conn, agent_id) == []


def test_the_push_rolls_back_with_the_delivery(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    _say(db_conn, agent_id, "a", "never committed")
    assert len(_pushes(db_conn, agent_id)) == 1

    db_conn.rollback()

    assert _pushes(db_conn, agent_id) == []


def test_a_switched_off_category_stages_no_alert_or_replace_but_still_retracts(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _agent(db_conn)
    first = _say(db_conn, agent_id, "a", "visible")
    assert len(_pushes(db_conn, agent_id)) == 1
    db_conn.execute(
        "INSERT INTO user_settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (SETTINGS_KEY, Jsonb({UNREAD_CATEGORY: False})),
    )

    newest = _say(db_conn, agent_id, "b", "while switched off")
    assert len(_pushes(db_conn, agent_id)) == 1
    advance_seen(db_conn, agent_id, newest)

    assert [action for action, *_ in _pushes(db_conn, agent_id)] == ["alert", "retract"]
    assert first < newest


def test_a_question_is_one_push_of_its_own_and_is_retracted_when_resolved(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _agent(db_conn)
    stage_question_asked(db_conn, agent_id=agent_id, question_id=31, title="Deploy now?")
    stage_question_asked(db_conn, agent_id=agent_id, question_id=32, title="Which region?")
    stage_question_resolved(db_conn, agent_id=agent_id, question_id=31, title="Deploy now?")

    assert _pushes(db_conn, agent_id) == [
        ("alert", "question:31", "Deploy now?", "Builder"),
        ("alert", "question:32", "Which region?", "Builder"),
        ("retract", "question:31", "Deploy now?", "Builder"),
    ]
