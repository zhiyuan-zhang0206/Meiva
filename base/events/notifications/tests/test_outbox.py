from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from base.db import create_agent
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus, event_bus_config_from_settings
from base.events.notifications.notices import stage_notice_notification
from base.events.notifications.outbox import republish_pending
from base.events.notifications.publish import announce_sync, deliver_sync


def _pool(db_url: str) -> ConnectionPool[psycopg.Connection[TupleRow]]:
    return ConnectionPool(db_url, min_size=1, max_size=1, open=True)


# meiva:note
# An `EventBus` whose publication is observed instead of sent. `drop` is the
# Redis outage the transport already models: `publish_best_effort_sync` reports
# the publication as failed by returning None, and the delivery must stay owed.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
class RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__(event_bus_config_from_settings())
        self.events: list[dict[str, Any]] = []
        self.drop = False

    def publish_best_effort_sync(
        self,
        payload: str,
        *,
        channel: str | None = None,
        decode_responses: bool = True,
        context: str = "",
    ) -> int | None:
        import json

        del channel, decode_responses, context
        if self.drop:
            return None
        self.events.append(json.loads(payload))
        return 1

    def notifications(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("role") == "notification"]


def _announced_at(conn: psycopg.Connection, notification_id: int) -> Any:
    row = conn.execute(
        "SELECT announced_at FROM notification_events WHERE id = %s", (notification_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def _age(conn: psycopg.Connection, notification_id: int, seconds: float) -> None:
    conn.execute(
        "UPDATE notification_events SET created_at = now() - make_interval(secs => %s) "
        "WHERE id = %s",
        (seconds, notification_id),
    )
    conn.commit()


# meiva:note
# A failed announce is not a delivery: the row stays unannounced, which is the
# outbox condition, and the same call succeeds once the transport is back.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_a_failed_announce_leaves_the_row_owed(db_conn: psycopg.Connection, db_url: str) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    bus = RecordingBus()
    bus.drop = True
    with _pool(db_url) as pool:
        record = deliver_sync(
            pool, category_id="core.needs_input", subject="needs an answer", agent_id=agent_id
        )
        assert record is not None
        assert announce_sync(pool, bus, record.id) is False
        assert _announced_at(db_conn, record.id) is None
        bus.drop = False
        assert announce_sync(pool, bus, record.id) is True
        assert _announced_at(db_conn, record.id) is not None
    assert [event["notification_id"] for event in bus.notifications()] == [record.id]


# meiva:note
# The crash case: a producer committed the delivery and died before publishing.
# The sweep is what makes that recoverable, and a second sweep must not push the
# same delivery again.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_the_sweep_announces_a_committed_but_unannounced_delivery(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    bus = RecordingBus()
    with _pool(db_url) as pool:
        record = deliver_sync(
            pool, category_id="core.needs_input", subject="needs an answer", agent_id=agent_id
        )
        assert record is not None
        # meiva:note
        # The producer died here: committed, never announced.
        # meiva:link base/events/notifications/docs/notifications.meiva.okf.md
        _age(db_conn, record.id, 120)
        assert republish_pending(pool, bus, min_age_seconds=30, limit=10) == 1
        assert _announced_at(db_conn, record.id) is not None
        assert republish_pending(pool, bus, min_age_seconds=30, limit=10) == 0
    assert [event["notification_id"] for event in bus.notifications()] == [record.id]


def test_a_fresh_row_is_left_to_its_producer(db_conn: psycopg.Connection, db_url: str) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    bus = RecordingBus()
    with _pool(db_url) as pool:
        record = deliver_sync(
            pool, category_id="core.needs_input", subject="needs an answer", agent_id=agent_id
        )
        assert record is not None
        assert republish_pending(pool, bus, min_age_seconds=30, limit=10) == 0
        assert _announced_at(db_conn, record.id) is None
    assert bus.notifications() == []


# meiva:note
# A notice's delivery is owed from the moment the notice commits, so the sweep
# recovers a notice whose post-commit announce never ran.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_the_sweep_recovers_a_committed_notice_with_no_announce(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    inserted = db_conn.execute(
        "INSERT INTO agent_notices "
        "(agent_id, local_id, title, content, priority, require_response, expire_at) "
        "VALUES (%s, 1, 'Need a decision', 'A or B', 'P1', TRUE, now() + interval '1 day') "
        "RETURNING id",
        (agent_id,),
    ).fetchone()
    assert inserted is not None
    notice_id = int(inserted[0])
    db_conn.commit()
    bus = RecordingBus()
    with _pool(db_url) as pool:
        with write_transaction(pool) as conn:
            staged = stage_notice_notification(conn, agent_id=agent_id, notice_id=notice_id)
        assert staged is not None
        # meiva:note
        # ... and the process died before announcing it.
        # meiva:link base/events/notifications/docs/notifications.meiva.okf.md
        _age(db_conn, staged, 120)
        assert republish_pending(pool, bus, min_age_seconds=30, limit=10) == 1
    delivered = bus.notifications()
    assert len(delivered) == 1
    assert delivered[0]["subject"] == "Need a decision"


# meiva:note
# A sweep reaches rows one at a time, and one row that raises must not hold the
# deliveries behind it: the failing row stays unannounced for the next sweep, the
# rest go out now. The failure is the transport's own — an exception out of the
# publish call, not the `None` it returns for a known Redis failure.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
class ExplodingBus(RecordingBus):
    def __init__(self, subject: str) -> None:
        super().__init__()
        self.subject = subject

    def publish_best_effort_sync(
        self,
        payload: str,
        *,
        channel: str | None = None,
        decode_responses: bool = True,
        context: str = "",
    ) -> int | None:
        import json

        if json.loads(payload).get("subject") == self.subject:
            raise RuntimeError("transport exploded")
        return super().publish_best_effort_sync(
            payload, channel=channel, decode_responses=decode_responses, context=context
        )


def test_one_failing_row_does_not_hold_the_rest_of_the_sweep(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    with _pool(db_url) as pool:
        records = [
            deliver_sync(
                pool,
                category_id="core.needs_input",
                subject=f"needs an answer {index}",
                agent_id=agent_id,
            )
            for index in range(3)
        ]
        ids = [record.id for record in records if record is not None]
        assert len(ids) == 3
        for notification_id in ids:
            _age(db_conn, notification_id, 120)
        broken = ids[1]
        assert (
            republish_pending(pool, ExplodingBus("needs an answer 1"), min_age_seconds=30, limit=10)
            == 2
        )
        assert _announced_at(db_conn, ids[0]) is not None
        assert _announced_at(db_conn, broken) is None
        assert _announced_at(db_conn, ids[2]) is not None
        assert republish_pending(pool, RecordingBus(), min_age_seconds=30, limit=10) == 1
        assert _announced_at(db_conn, broken) is not None
