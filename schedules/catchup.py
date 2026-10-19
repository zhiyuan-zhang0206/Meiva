from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from base.clock import Clock
from base.db import Database


_Payload = TypeVar("_Payload")


# meiva:note
# The cluster wall clock (`MEIVA_TIMEZONE`, cluster-pinned), never the host's OS timezone:
# the fleet fires at one instant wherever each machine sits, and a weekly cron read
# host-locally lands the run on the wrong calendar day, not merely at the wrong hour. A
# schedule reads it when it runs, so a changed value waits for its next restart.
# meiva:link schedules/README.md
def cluster_timezone() -> str:
    return Clock.from_settings().timezone


def _schedule_id(explicit: int | None) -> int:
    schedule_id = int(os.environ["MEIVA_SCHEDULE_ID"]) if explicit is None else explicit
    if schedule_id <= 0:
        raise ValueError(f"schedule_id must be positive, got {schedule_id}")
    return schedule_id


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must carry tzinfo")
    return value.astimezone(UTC)


# meiva:note
# The durable claim commits before the callback, so concurrent runners and later restarts
# get one winner and the callback receives the claimed UTC slot; a crash after the claim
# loses that slot instead of retrying it, and a callback failure still leaves it committed.
# meiva:link schedules/README.md
def fire_slot_once(
    db: Database,
    slot_fire_at: datetime,
    payload: _Payload,
    *,
    fire: Callable[[datetime, _Payload], None],
    schedule_id: int | None = None,
) -> bool:
    resolved_id = _schedule_id(schedule_id)
    slot = _as_utc(slot_fire_at, field="slot_fire_at")
    with db.write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedule_fire_log (schedule_id, slot_fire_at) "
            "VALUES (%s, %s) ON CONFLICT (schedule_id, slot_fire_at) DO NOTHING "
            "RETURNING id",
            (resolved_id, slot),
        )
        claimed = cur.fetchone() is not None
    if not claimed:
        return False
    fire(slot, payload)
    return True
