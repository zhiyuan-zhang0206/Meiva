# meiva:note
# The effect half of the watch's own liveness: read the newest round back from `telemetry_events`
# and open one card when it is too old. It runs on its own cron trigger, five minutes after each
# round, so a round that stops arriving is reported even while the loop that should run it stays up.
#
# Kept out of the hyphenated entry script so the behaviour is importable and testable; the entry
# only wires the clock and the task API.
# meiva:link schedules/README.md

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from base.db import Database
from schedules.plans.watch.main_red import WATCH_STALE_AFTER, Card

__all__ = ["EVENT_NAME", "liveness_once", "newest_round_at"]

EVENT_NAME = "main_red_watch_round"

# meiva:note
# The name is spelled into the query rather than bound: `gateway/inspect/tests/
# test_persisted_events_cover_readers.py` proves this event has a reader by reading the literal,
# and a bound parameter (`event_name = %s`) is invisible to that scan — which is how this reader
# went unseen. The assert keeps the two spellings from drifting apart.
_NEWEST_ROUND_SQL = "SELECT max(ts) FROM telemetry_events WHERE event_name = 'main_red_watch_round'"
assert EVENT_NAME in _NEWEST_ROUND_SQL, "the query and EVENT_NAME name different events"


# meiva:note
# `main_red_watch_round` is declared `persist`, so the row is in `telemetry_events`; reading it
# here is what makes "the watch stopped" an observation instead of an assumption.
def newest_round_at(database: Database) -> datetime | None:
    with database.connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(_NEWEST_ROUND_SQL, ())
        row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def liveness_once(
    slot: datetime,
    *,
    database: Database,
    open_card: Callable[[Card], int],
    close_card: Callable[[], None],
) -> int | None:
    last = newest_round_at(database)
    card = Card.for_stale_watch(last, slot, stale_after=WATCH_STALE_AFTER)
    if card is None:
        # meiva:note
        # A round is on record again, so the card this check opened has been answered. Closing it
        # here is what makes the card's own sentence -- "closes when a round lands again" -- true;
        # opening without a closer leaves a promise nobody keeps.
        close_card()
        seen = last.isoformat() if last is not None else "none on record"
        print(f"[{slot.isoformat()}] watch alive: newest round {seen}", flush=True)
        return None
    task_id = open_card(card)
    print(f"[{slot.isoformat()}] watch STALE: opened card #{task_id}", flush=True)
    return task_id
