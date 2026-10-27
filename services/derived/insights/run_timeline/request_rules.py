# meiva:note
# What the run-timeline API and its backfill command accept and refuse, as pure functions of the
# query they were given: the window's defaults and ordering, the message range, the agent ids. A
# refusal is a `TimelineRequestError` carrying the status and detail; the HTTP layer turns it into
# the response, so nothing here knows about FastAPI.
# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md

from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import datetime, timedelta

_EMPTY_WINDOW = timedelta(hours=24)


# meiva:note
# A request the timeline API refuses. `detail` is the message (or the structured body of a
# transient index failure) and `headers` any the response must carry.
class TimelineRequestError(Exception):
    def __init__(
        self,
        status: int,
        detail: str | dict[str, str],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.headers = headers


def _require_zone(name: str, value: datetime | None) -> None:
    if value is not None and value.tzinfo is None:
        raise TimelineRequestError(422, f"{name} must include a timezone offset")


# meiva:note
# The checks on the window query before any snapshot is read: `recent_hours` stands alone, the
# bounds carry a zone, and two given bounds are in order.
def check_window_query(
    from_: datetime | None, to: datetime | None, recent_hours: int | None
) -> None:
    if recent_hours is not None and (from_ is not None or to is not None):
        raise TimelineRequestError(422, "recent_hours cannot be combined with from/to")
    _require_zone("from", from_)
    _require_zone("to", to)
    if from_ is not None and to is not None and from_ >= to:
        raise TimelineRequestError(422, "from must be earlier than to")


# meiva:note
# The window to serve: an omitted end is the end of the agent's lifetime (or `now` for an agent
# with none, over the last day), an omitted start is the lifetime's start or `recent_hours`
# before the end, whichever is later. A window that ends up empty or reversed is refused.
def resolve_window(
    lifetime: tuple[datetime, datetime] | None,
    from_: datetime | None,
    to: datetime | None,
    now: datetime,
    recent_hours: int | None,
) -> tuple[datetime, datetime]:
    default = lifetime or (now - _EMPTY_WINDOW, now)
    end = to or default[1]
    start = from_ or (
        max(default[0], end - timedelta(hours=recent_hours))
        if recent_hours is not None
        else default[0]
    )
    if start >= end:
        raise TimelineRequestError(422, "from must be earlier than to")
    return start, end


def check_message_range(start: int, end: int) -> None:
    if end < start:
        raise TimelineRequestError(422, "end must not be before start")


# meiva:note
# The last message of the page that starts at `start`: at most `limit` messages, none past `end`.
# `count` is the history length of the snapshot; `end` must lie inside it.
def message_page_stop(start: int, end: int, limit: int, count: int) -> int:
    if end >= count:
        raise TimelineRequestError(404, f"message {end} not found: history has {count}")
    return min(end, start + limit - 1)


# meiva:note
# The `type` of the backfill command's `agent_ids` argument: a positive integer.
def positive_agent_id(text: str) -> int:
    try:
        agent_id = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"agent ids must be integers: {text!r}") from None
    if agent_id <= 0:
        raise argparse.ArgumentTypeError("agent ids must be positive")
    return agent_id


# meiva:note
# An agent named twice is prepared once, in the order it was first named.
def distinct_in_order(agent_ids: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(agent_ids))
