# meiva:note
# What the schedule tests share: a schedule row to claim slots against, a reader for the claims
# they made, and the inputs a runner would lend a script.
#
# The tests run against the throwaway Postgres; `schedule_fire_log` rows reference a `schedules`
# row, so each test inserts one and reads back which slots were durably claimed.

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import psycopg

from base.cluster.machine import MachineIdentityReader
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import EventPipeline

__all__ = ["borrowed_inputs", "claimed_slots", "insert_schedule"]


def insert_schedule(conn: psycopg.Connection, *, name: str, created_at: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, created_at) "
            "VALUES (%s, 'x', 'python schedule.py', true, 'stopped', %s) RETURNING id",
            (name, created_at),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def claimed_slots(conn: psycopg.Connection, schedule_id: int) -> list[datetime]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT slot_fire_at FROM schedule_fire_log "
            "WHERE schedule_id = %s ORDER BY slot_fire_at",
            (schedule_id,),
        )
        return [row[0] for row in cur.fetchall()]


def borrowed_inputs(
    database: Database,
    producer: Callable[[], EventPipeline],
    machine: MachineIdentityReader,
    source_root: Path,
) -> ScheduleInputs:
    return ScheduleInputs(
        lambda: database,
        producer,
        LoadedCommit(source_root, None),
        machine=machine,
        home=source_root / ".meiva",
    )
