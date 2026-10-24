# meiva:note
# The status snapshot's session rows, decided from what the backends listed.
#
# Listings and instants go in as values; the rows come out. No backend is asked anything.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ops.cluster_status.listing import SessionStart, session_rows

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=_SHANGHAI)


def _epoch(moment: datetime) -> float:
    return moment.timestamp()


# meiva:note
# Session times render in the cluster zone (user ruling 2026-08-27), never the host OS zone: a
# runner whose OS zone differs must show the same wall clock as the gateway.
def test_a_session_is_stamped_in_the_zone_of_the_instant_it_is_listed_at() -> None:
    (row,) = session_rows([SessionStart("meiva-gateway", 1000.0)], _NOW)

    # meiva:note
    # Epoch 1000 is 1970-01-01 00:16:40 UTC, which is 08:16:40 on the Shanghai wall clock.
    assert row.created_at == datetime(1970, 1, 1, 8, 16, 40, tzinfo=_SHANGHAI)
    assert row.created_at is not None
    assert row.created_at.utcoffset() == timedelta(hours=8)


def test_uptime_is_the_time_since_launch() -> None:
    launched = _NOW - timedelta(hours=2, seconds=5)

    (row,) = session_rows([SessionStart("meiva-gateway", _epoch(launched))], _NOW)

    assert row.uptime_seconds == 2 * 3600 + 5


@pytest.mark.parametrize("epoch", [None, 1e30, -1e30, float("nan")])
def test_a_launch_time_the_backend_cannot_give_leaves_the_row_without_one(
    epoch: float | None,
) -> None:
    (row,) = session_rows([SessionStart("meiva-gateway", epoch)], _NOW)

    assert (row.created_at, row.uptime_seconds) == (None, 0)


# meiva:note
# A bare `meiva-agent-<id>` (and a boot launcher) is not a session: counting it ghosted 26 of 38
# agent groups (#2330). An agent's shell is a session.
def test_agent_processes_are_pid_records_not_sessions() -> None:
    rows = session_rows(
        [
            SessionStart("meiva-agent-9", None),
            SessionStart("meiva-main-agent-9", None),
            SessionStart("meiva-boot-3-4-5", None),
            SessionStart("meiva-agent-7-shell-0", None),
            SessionStart("meiva-agent-7-shell-0-watcher", None),
            SessionStart("meiva-gateway", None),
        ],
        _NOW,
    )

    assert [row.name for row in rows] == [
        "meiva-agent-7-shell-0",
        "meiva-agent-7-shell-0-watcher",
        "meiva-gateway",
    ]


def test_rows_come_back_sorted_by_name() -> None:
    rows = session_rows(
        [SessionStart(name, None) for name in ("meiva-ops", "meiva-gateway", "meiva-labeler")], _NOW
    )

    assert [row.name for row in rows] == ["meiva-gateway", "meiva-labeler", "meiva-ops"]


def test_a_name_listed_twice_keeps_the_last_entry() -> None:
    rows = session_rows([SessionStart("meiva-ops", 1000.0), SessionStart("meiva-ops", None)], _NOW)

    assert [(row.name, row.created_at) for row in rows] == [("meiva-ops", None)]


def test_nothing_listed_is_no_rows() -> None:
    assert session_rows([], _NOW) == ()


def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        session_rows([], datetime.fromisoformat("2026-10-24T12:00:00"))


def test_the_utc_instant_stamps_utc() -> None:
    (row,) = session_rows([SessionStart("meiva-ops", 0.0)], datetime(2026, 1, 1, tzinfo=UTC))

    assert row.created_at == datetime(1970, 1, 1, tzinfo=UTC)
