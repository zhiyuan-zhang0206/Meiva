from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from base.db import create_agent
from base.db.transaction import write_transaction
from base.events.notifications.outbox import republish_pending
from base.events.notifications.publish import announce_sync, stage_question_asked


def _pool(db_url: str) -> ConnectionPool[psycopg.Connection[TupleRow]]:
    return ConnectionPool(db_url, min_size=1, max_size=1, open=True)


# meiva:note
# An `EventBus` whose publication is observed instead of sent. `drop` is the
# Redis outage the transport already models: `publish_best_effort_sync` reports
# the publication as failed by returning None, and the delivery must stay owed.
# meiva:link base/events/notifications/README.md
class RecordingBus:
    def __init__(self) -> None:
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


def _stage(
    pool: ConnectionPool[psycopg.Connection[TupleRow]], agent_id: int, notice_id: int
) -> int:
    with write_transaction(pool) as conn:
        stage_question_asked(
            conn, agent_id=agent_id, notice_id=notice_id, title=f"Question {notice_id}?"
        )
        row = conn.execute("SELECT max(id) FROM notification_events").fetchone()
    assert row is not None
    return int(row[0])


# meiva:note
# A failed announce is not a delivery: the row stays unannounced, which is the
# outbox condition, and the same call succeeds once the transport is back.
# meiva:link base/events/notifications/README.md
def test_a_failed_announce_leaves_the_row_owed(db_conn: psycopg.Connection, db_url: str) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    bus = RecordingBus()
    bus.drop = True
    with _pool(db_url) as pool:
        notification_id = _stage(pool, agent_id, 1)
        assert announce_sync(pool, bus, notification_id) is False
        assert _announced_at(db_conn, notification_id) is None
        bus.drop = False
        assert announce_sync(pool, bus, notification_id) is True
        assert _announced_at(db_conn, notification_id) is not None
    assert [event["notification_id"] for event in bus.notifications()] == [notification_id]


# meiva:note
# Producers never announce: the committed row is owed to the sweep, the live event
# carries the push's action and tag, and a second sweep must not push it again.
# meiva:link base/events/notifications/README.md
def test_the_sweep_announces_a_committed_push_once(
    db_conn: psycopg.Connection, db_url: str
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.commit()
    bus = RecordingBus()
    with _pool(db_url) as pool:
        notification_id = _stage(pool, agent_id, 5)
        assert republish_pending(pool, bus, limit=10) == 1
        assert _announced_at(db_conn, notification_id) is not None
        assert republish_pending(pool, bus, limit=10) == 0
    [event] = bus.notifications()
    assert event["notification_id"] == notification_id
    assert (event["action"], event["tag"], event["subject"]) == (
        "alert",
        "question:5",
        "Question 5?",
    )


# meiva:note
# A sweep reaches rows one at a time, and one row that raises must not hold the
# deliveries behind it: the failing row stays unannounced for the next sweep, the
# rest go out now. The failure is the transport's own — an exception out of the
# publish call, not the `None` it returns for a known Redis failure.
# meiva:link base/events/notifications/README.md
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
        ids = [_stage(pool, agent_id, index) for index in range(3)]
        broken = ids[1]
        assert republish_pending(pool, ExplodingBus("Question 1?"), limit=10) == 2
        assert _announced_at(db_conn, ids[0]) is not None
        assert _announced_at(db_conn, broken) is None
        assert _announced_at(db_conn, ids[2]) is not None
        assert republish_pending(pool, RecordingBus(), limit=10) == 1
        assert _announced_at(db_conn, broken) is not None
