from __future__ import annotations

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from base.db import create_agent
from base.events.live.bus import EventBus
from base.events.notifications.notices import deliver_notice_notification


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


def _categories(conn: psycopg.Connection, agent_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT category FROM notification_events WHERE agent_id = %s", (agent_id,)
    ).fetchall()
    return [row[0] for row in rows]


async def test_a_response_notice_is_delivered(
    db_conn: psycopg.Connection, db_url: str, event_bus: EventBus
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    notice_id = _notice(db_conn, agent_id, require_response=True)
    with _pool(db_url) as pool:
        delivered = await deliver_notice_notification(
            pool, event_bus, agent_id=agent_id, notice_id=notice_id
        )
    assert delivered is not None
    row = db_conn.execute(
        "SELECT category, subject, dedupe_key FROM notification_events WHERE agent_id = %s",
        (agent_id,),
    ).fetchone()
    assert row == ("core.needs_input", "Need a decision", f"notice:{notice_id}")


async def test_an_fyi_notice_is_never_a_notification(
    db_conn: psycopg.Connection, db_url: str, event_bus: EventBus
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    notice_id = _notice(db_conn, agent_id, require_response=False)
    with _pool(db_url) as pool:
        assert (
            await deliver_notice_notification(
                pool, event_bus, agent_id=agent_id, notice_id=notice_id
            )
            is None
        )
    assert _categories(db_conn, agent_id) == []


async def test_a_second_post_of_the_same_notice_does_not_notify_twice(
    db_conn: psycopg.Connection, db_url: str, event_bus: EventBus
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    notice_id = _notice(db_conn, agent_id, require_response=True)
    with _pool(db_url) as pool:
        first = await deliver_notice_notification(
            pool, event_bus, agent_id=agent_id, notice_id=notice_id
        )
        second = await deliver_notice_notification(
            pool, event_bus, agent_id=agent_id, notice_id=notice_id
        )
    assert first is not None
    assert second is None
