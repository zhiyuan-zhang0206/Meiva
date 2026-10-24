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


# meiva:note
# What the owner reads when its notice expired: which notice, when it lapsed, that nothing is
# pending any more, and how to ask again. Deliberately not shaped like a reply ("Re: ..."),
# because the user did not answer and the agent must not mistake a lapse for one.
def notice_expired_message(local_id: int, title: str, expired_at: str) -> str:
    return (
        f'Notice #{local_id} "{title}" reached its expiry at {expired_at} '
        "without a response from the user, so it is resolved as expired. It no longer "
        "waits for an answer and no longer occupies your one open notice; post a new "
        "one with meiva.ui.notify(title, ..., idempotency_key=...) if it still matters."
    )


# meiva:note
# What the owner reads when its page expired. A `show()` page keeps its server in the agent's own
# process, so that owner is told to stop it and release the port; a served page is re-served.
def page_expired_message(name: str, agent_id: int, *, shown_by_agent: bool) -> str:
    head = f"Page {name!r} (agent {agent_id}) was reclaimed after its TTL expired."
    if shown_by_agent:
        return (
            f"{head} Stop the page's HTTP server to release its port; "
            "re-show with meiva.ui.show() to republish."
        )
    return f"{head} Serve it again with meiva.ui.serve() to republish."
