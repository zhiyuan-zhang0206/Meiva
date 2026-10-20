from __future__ import annotations

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.db import create_agent
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from base.events.notifications.notices import stage_notice_notification
from base.events.notifications.publish import announce_sync


def _pool(db_url: str) -> ConnectionPool[psycopg.Connection[TupleRow]]:
    return ConnectionPool(db_url, min_size=1, max_size=1, open=True)


def _notice(conn: psycopg.Connection, agent_id: int, *, require_response: bool) -> int:
    row = conn.execute(
        "INSERT INTO agent_notices "
        "(agent_id, local_id, title, content, priority, require_response, expire_at) "
        "VALUES (%s, 1, 'Need a decision', 'A or B', 'P1', %s, now() + interval '1 day') "
        "RETURNING id",
        (agent_id, require_response),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def _stage(
    pool: ConnectionPool[psycopg.Connection[TupleRow]], agent_id: int, notice_id: int
) -> int | None:
    with write_transaction(pool) as conn:
        return stage_notice_notification(conn, agent_id=agent_id, notice_id=notice_id)


def _deliveries(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str | None]]:
    rows = conn.execute(
        "SELECT category, dedupe_key FROM notification_events WHERE agent_id = %s ORDER BY id",
        (agent_id,),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


# meiva:note
# Staging is the producer's whole job: the delivery row commits with the notice,
# and `announced_at IS NULL` says no live event has gone out yet.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_a_response_notice_stages_one_unannounced_delivery(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    notice_id = _notice(db_conn, agent_id, require_response=True)
    with _pool(db_url) as pool:
        staged = _stage(pool, agent_id, notice_id)
    assert staged is not None
    row = db_conn.execute(
        "SELECT category, subject, dedupe_key, announced_at FROM notification_events WHERE id = %s",
        (staged,),
    ).fetchone()
    assert row is not None
    category, subject, dedupe_key, announced_at = row
    assert (category, subject) == ("core.needs_input", "Need a decision")
    assert isinstance(dedupe_key, str)
    assert dedupe_key.startswith(f"notice:{notice_id}:")
    assert announced_at is None


# meiva:note
# The delivery names the notice it is about: a transport that can open a link
# opens this notice, not the inbox in general, so a tap answers "which one is
# this?" instead of leaving the user to search a queue.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_a_notice_delivery_links_to_the_notice(db_conn: psycopg.Connection, db_url: str) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    notice_id = _notice(db_conn, agent_id, require_response=True)
    with _pool(db_url) as pool:
        staged = _stage(pool, agent_id, notice_id)
    assert staged is not None
    row = db_conn.execute(
        "SELECT href FROM notification_events WHERE id = %s", (staged,)
    ).fetchone()
    assert row == (f"/fleet?notice={notice_id}",)


def test_an_fyi_notice_is_never_a_notification(db_conn: psycopg.Connection, db_url: str) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    notice_id = _notice(db_conn, agent_id, require_response=False)
    with _pool(db_url) as pool:
        assert _stage(pool, agent_id, notice_id) is None
    assert _deliveries(db_conn, agent_id) == []


def test_adding_a_question_to_the_same_notice_notifies_again(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    notice_id = _notice(db_conn, agent_id, require_response=True)
    with _pool(db_url) as pool:
        first = _stage(pool, agent_id, notice_id)
        db_conn.execute(
            "UPDATE agent_notices SET question = %s WHERE id = %s",
            (Jsonb({"type": "text"}), notice_id),
        )
        db_conn.commit()
        second = _stage(pool, agent_id, notice_id)
    assert first is not None
    assert second is not None
    assert [category for category, _key in _deliveries(db_conn, agent_id)] == [
        "core.needs_input",
        "core.needs_input",
    ]


def test_a_second_post_of_the_same_notice_does_not_notify_twice(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    notice_id = _notice(db_conn, agent_id, require_response=True)
    with _pool(db_url) as pool:
        first = _stage(pool, agent_id, notice_id)
        second = _stage(pool, agent_id, notice_id)
    assert first is not None
    assert second is None


def test_announcing_marks_the_row_it_staged(
    db_conn: psycopg.Connection, db_url: str, event_bus: EventBus
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    notice_id = _notice(db_conn, agent_id, require_response=True)
    with _pool(db_url) as pool:
        staged = _stage(pool, agent_id, notice_id)
        assert staged is not None
        assert announce_sync(pool, event_bus, staged) is True
        row = db_conn.execute(
            "SELECT announced_at FROM notification_events WHERE id = %s", (staged,)
        ).fetchone()
        assert row is not None and row[0] is not None
        assert announce_sync(pool, event_bus, staged) is False
