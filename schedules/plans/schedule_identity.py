# meiva:note
# The schedule row id a runner hands a script in `MEIVA_SCHEDULE_ID`. The entry reads the
# variable once and validates it here; every claim carries the result, so a missing or
# non-positive id stops the script before it can write a claim for the wrong schedule.
# meiva:link schedules/README.md

from __future__ import annotations

__all__ = ["parse_schedule_id"]


def parse_schedule_id(raw: str | None) -> int:
    if raw is None or not raw.strip():
        raise ValueError("MEIVA_SCHEDULE_ID is not set")
    try:
        schedule_id = int(raw)
    except ValueError as exc:
        raise ValueError(f"MEIVA_SCHEDULE_ID must be an integer, got {raw!r}") from exc
    if schedule_id <= 0:
        raise ValueError(f"schedule_id must be positive, got {schedule_id}")
    return schedule_id
