from __future__ import annotations

import uuid

import psycopg
import pytest

from base.events.notifications.publish import deliver_in_transaction


def test_delivery_records_one_row(db_conn: psycopg.Connection) -> None:
    record = deliver_in_transaction(
        db_conn,
        category_id="core.needs_input",
        subject="Agent #1 is waiting on your answer",
        agent_id=1,
    )

    assert record is not None
    assert record.id > 0
    assert record.category == "core.needs_input"
    assert record.importance == "high"


def test_disabled_category_records_nothing(db_conn: psycopg.Connection) -> None:
    record = deliver_in_transaction(
        db_conn,
        category_id="core.needs_input",
        subject="Agent #1 is waiting on your answer",
        overrides={"core.needs_input": False},
    )

    assert record is None


def test_same_key_inside_the_window_is_dropped(db_conn: psycopg.Connection) -> None:
    key = f"notice:{uuid.uuid4()}"
    first = deliver_in_transaction(
        db_conn,
        category_id="core.needs_input",
        subject="Agent #1 is waiting on your answer",
        dedupe_key=key,
    )
    second = deliver_in_transaction(
        db_conn,
        category_id="core.needs_input",
        subject="Agent #1 is waiting on your answer",
        dedupe_key=key,
    )

    assert first is not None
    assert second is None


def test_unknown_category_is_rejected(db_conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="unknown notification category"):
        deliver_in_transaction(db_conn, category_id="plugin.unknown", subject="x")


def test_empty_subject_is_rejected(db_conn: psycopg.Connection) -> None:
    with pytest.raises(ValueError, match="subject"):
        deliver_in_transaction(db_conn, category_id="core.needs_input", subject="  ")
