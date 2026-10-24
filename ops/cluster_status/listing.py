# meiva:note
# The status snapshot's session rows, decided from what the session backends listed.
#
# The shell asks each backend for its live session names and their launch epochs; this module
# keeps the cluster's sessions, stamps each with a creation time and an uptime in the zone of
# the `now` it is given, and does no I/O.

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo

from ops.rpc_schemas import SessionInfo

__all__ = ["SessionStart", "session_rows"]

# meiva:note
# A bare agent main-process session (`meiva-agent-<id>`). Agent processes are pid records, not
# sessions — they are filtered out so the session count / agent groups stay session-shaped (a
# P1 regression in #2330 put them back in and ghosted 26 of 38 agent_groups).
_AGENT_PROCESS_RE = re.compile(
    r"^meiva-(?:[a-z0-9]+-)*(?:agent-\d+|boot-\d+-(?:\d+-\d+|[a-f0-9]{32}))$"
)


# meiva:note
# One listed session: its name and its launch epoch in seconds, None when the backend keeps no
# timestamp for it.
@dataclass(frozen=True)
class SessionStart:
    name: str
    started_at: float | None


def _created_at(started_at: float | None, zone: tzinfo) -> datetime | None:
    if started_at is None:
        return None
    try:
        return datetime.fromtimestamp(started_at, tz=zone)
    except (OSError, OverflowError, ValueError):
        return None


# meiva:note
# The session rows for everything the backends listed, sorted by name.
#
# `now` is the one instant the snapshot is taken at, already in the zone the rows render in
# (the cluster's wall-clock zone, never the host OS zone): each `created_at` is stamped in that
# zone and each uptime is measured against it. A name listed twice keeps its last entry, so the
# caller lists the service backend first and the shell backend after it. A launch epoch the
# platform cannot represent leaves the row without a creation time and with zero uptime.
#
# Raises:
#     ValueError: `now` is naive.
def session_rows(listed: Sequence[SessionStart], now: datetime) -> tuple[SessionInfo, ...]:
    if now.tzinfo is None:
        raise ValueError("session rows need a timezone-aware `now`")
    rows: dict[str, SessionInfo] = {}
    for entry in listed:
        if _AGENT_PROCESS_RE.match(entry.name):
            continue
        created = _created_at(entry.started_at, now.tzinfo)
        uptime = int((now - created).total_seconds()) if created is not None else 0
        rows[entry.name] = SessionInfo(name=entry.name, created_at=created, uptime_seconds=uptime)
    return tuple(sorted(rows.values(), key=lambda row: row.name))
