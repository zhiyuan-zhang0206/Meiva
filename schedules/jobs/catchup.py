from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from base.clock import Clock
from base.db import Database


# meiva:note
# The cluster wall clock (`MEIVA_TIMEZONE`, cluster-pinned), never the host's OS timezone:
# the fleet fires at one instant wherever each machine sits, and a weekly cron read
# host-locally lands the run on the wrong calendar day, not merely at the wrong hour. The
# entry reads it once and hands the value down, so a changed value waits for the schedule's
# next restart.
# meiva:link schedules/README.md
def cluster_timezone() -> str:
    return Clock.from_settings().timezone


def _as_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must carry tzinfo")
    return value.astimezone(UTC)


# meiva:note
# What a claimed slot's callback returned, with the UTC slot it ran for.
@dataclass(frozen=True)
class Fired[R]:
    slot: datetime
    result: R


# meiva:note
# The durable claim commits before the callback, so concurrent runners and later restarts
# get one winner and the callback receives the claimed UTC slot; a crash after the claim
# loses that slot instead of retrying it, and a callback failure still leaves it committed.
# The loser gets `None`. `schedule_id` is the schedule row the claim belongs to: the entry
# reads `MEIVA_SCHEDULE_ID` once and passes it down.
# meiva:link schedules/README.md
def fire_slot_once[P, R](
    db: Database,
    slot_fire_at: datetime,
    payload: P,
    *,
    fire: Callable[[datetime, P], R],
    schedule_id: int,
) -> Fired[R] | None:
    if schedule_id <= 0:
        raise ValueError(f"schedule_id must be positive, got {schedule_id}")
    slot = _as_utc(slot_fire_at, field="slot_fire_at")
    with db.write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedule_fire_log (schedule_id, slot_fire_at) "
            "VALUES (%s, %s) ON CONFLICT (schedule_id, slot_fire_at) DO NOTHING "
            "RETURNING id",
            (schedule_id, slot),
        )
        claimed = cur.fetchone() is not None
    if not claimed:
        return None
    return Fired(slot, fire(slot, payload))
