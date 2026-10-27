# meiva:note
# The durable slot claim against the throwaway Postgres: one winner per slot, the claim
# committed before the callback runs, and the cluster timezone the entry reads once.

from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest

from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from schedules.jobs.catchup import Fired, cluster_timezone, fire_slot_once
from schedules.tests.support.schedule_support import claimed_slots, insert_schedule

_CREATED = datetime(2026, 9, 6, 9, 30, tzinfo=UTC)


def _claim_worker(
    schedule_id: int,
    slot: datetime,
    ready: Any,
    start: Any,
    outcomes: Any,
    database_gate: ProcessDbGate,
) -> None:
    ready.put(True)
    if not start.wait(timeout=10):
        outcomes.put("timeout")
        return
    fired = fire_slot_once(
        Database.from_settings(gate=database_gate),
        slot,
        "payload",
        fire=lambda _slot, _payload: outcomes.put("fired"),
        schedule_id=schedule_id,
    )
    outcomes.put("claimed" if fired is not None else "lost")


def test_concurrent_processes_execute_a_slot_at_most_once(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
) -> None:
    assert database_gate.min_read_due(), "spawn copies a fresh admission budget"
    schedule_id = insert_schedule(db_conn, name="catch-up-race", created_at=_CREATED)
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    outcomes = context.Queue()
    workers = [
        context.Process(
            target=_claim_worker,
            args=(schedule_id, slot, ready, start, outcomes),
            kwargs={"database_gate": database_gate},
        )
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()
    for _ in workers:
        assert ready.get(timeout=15) is True
    start.set()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    observed = sorted(outcomes.get(timeout=5) for _ in range(3))
    assert observed == ["claimed", "fired", "lost"]
    assert claimed_slots(db_conn, schedule_id) == [slot]


def test_winner_receives_utc_slot_and_payload_after_claim_commits(
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    schedule_id = insert_schedule(db_conn, name="catch-up-winner", created_at=_CREATED)
    local_slot = datetime(2026, 9, 6, 18, 0, tzinfo=timezone(timedelta(hours=8)))
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    payload = object()

    def fire(claimed: datetime, received: object) -> list[datetime]:
        assert claimed.tzinfo is UTC
        assert received is payload
        return claimed_slots(db_conn, schedule_id)

    first = fire_slot_once(database, local_slot, payload, fire=fire, schedule_id=schedule_id)
    second = fire_slot_once(database, slot, payload, fire=fire, schedule_id=schedule_id)

    assert first == Fired(slot, [slot])
    assert second is None


def test_nested_dispatch_keeps_each_callbacks_explicit_slot(
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    schedule_id = insert_schedule(db_conn, name="catch-up-nested", created_at=_CREATED)
    outer_slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    inner_slot = datetime(2026, 9, 6, 11, 0, tzinfo=UTC)

    def outer_fire(
        slot: datetime, payload: str
    ) -> tuple[datetime, str, Fired[tuple[datetime, str]] | None]:
        inner = fire_slot_once(
            database,
            inner_slot,
            "inner",
            schedule_id=schedule_id,
            fire=lambda slot, payload: (slot, payload),
        )
        return slot, payload, inner

    fired = fire_slot_once(database, outer_slot, "outer", schedule_id=schedule_id, fire=outer_fire)

    assert fired == Fired(
        outer_slot, (outer_slot, "outer", Fired(inner_slot, (inner_slot, "inner")))
    )
    assert claimed_slots(db_conn, schedule_id) == [outer_slot, inner_slot]


def test_claim_survives_fire_failure(
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    schedule_id = insert_schedule(db_conn, name="catch-up-failure", created_at=_CREATED)
    slot = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)

    def failing(_slot: datetime, _payload: None) -> None:
        raise RuntimeError("after claim")

    with pytest.raises(RuntimeError, match="after claim"):
        fire_slot_once(database, slot, None, schedule_id=schedule_id, fire=failing)

    retried = fire_slot_once(
        database, slot, None, schedule_id=schedule_id, fire=lambda slot, _payload: slot
    )
    assert retried is None
    assert claimed_slots(db_conn, schedule_id) == [slot]


@pytest.mark.parametrize("schedule_id", [0, -1])
def test_a_non_positive_schedule_id_claims_nothing(database: Database, schedule_id: int) -> None:
    with pytest.raises(ValueError, match="schedule_id must be positive"):
        fire_slot_once(
            database,
            datetime(2026, 9, 6, 10, 0, tzinfo=UTC),
            None,
            schedule_id=schedule_id,
            fire=lambda slot, _payload: slot,
        )


def test_a_naive_slot_is_refused(database: Database) -> None:
    with pytest.raises(ValueError, match="slot_fire_at must carry tzinfo"):
        fire_slot_once(
            database,
            datetime(2026, 9, 6, 10, 0, tzinfo=UTC).replace(tzinfo=None),
            None,
            schedule_id=1,
            fire=lambda slot, _payload: slot,
        )


def test_cluster_timezone_is_the_cluster_setting() -> None:
    assert cluster_timezone() == settings.general.timezone
