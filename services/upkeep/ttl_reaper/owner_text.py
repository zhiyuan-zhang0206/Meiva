# meiva:note
# The words of the messages the reaper writes into an owner's inbox.
#
# Every function takes the facts it renders as values, including the timezone and the zone's
# current date, so the text is a function of its arguments and a test reads it without a clock.
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


# meiva:note
# A TTL duration as a compact string: 1h, 30m, 90s. A negative (clock-skewed) duration reads 0s.
def human_ttl(seconds: float) -> str:
    whole = max(0, int(seconds))
    if whole >= 3600 and whole % 3600 == 0:
        return f"{whole // 3600}h"
    if whole >= 60 and whole % 60 == 0:
        return f"{whole // 60}m"
    return f"{whole}s"


# meiva:note
# Local wall-clock for a message: HH:MM, or MM-DD HH:MM when the moment is not on `today`
# (a TTL can cross midnight). `zone=None` is the host zone (``dt.astimezone(None)``), the
# base/config contract for "no zone configured"; `today` is the current date in that same zone.
def wall_clock(dt: datetime, *, zone: ZoneInfo | None, today: date) -> str:
    local = dt.astimezone(zone)
    stamp = local.strftime("%H:%M")
    if local.date() != today:
        stamp = f"{local.strftime('%m-%d')} {stamp}"
    return stamp
