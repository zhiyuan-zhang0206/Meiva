from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from meiva.agents.row_text import relative_age

_NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s ago"),
        (59.999999, "59s ago"),
        (60, "1m ago"),
        (3599.999999, "59m ago"),
        (3600, "1h ago"),
        (86399.999999, "23h ago"),
        (86400, "1d ago"),
        (172800, "2d ago"),
        (-0.999999, "0s ago"),
        (-1.999999, "-1s ago"),
        (-60, "-60s ago"),
        (-3600, "-3600s ago"),
    ],
)
def test_relative_age_keeps_unit_boundaries_and_signed_truncation(
    seconds: float, expected: str
) -> None:
    assert relative_age(_NOW - timedelta(seconds=seconds), now=_NOW) == expected


@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        ("2026-03-08T01:30:00-08:00", "2026-03-08T03:30:00-07:00"),
        ("2026-11-01T01:30:00-07:00", "2026-11-01T01:30:00-08:00"),
    ],
    ids=["spring-forward", "fall-fold"],
)
def test_relative_age_counts_elapsed_time_across_dst(earlier: str, later: str) -> None:
    zone = ZoneInfo("America/Los_Angeles")
    moment = datetime.fromisoformat(earlier).astimezone(zone)
    now = datetime.fromisoformat(later).astimezone(zone)
    assert relative_age(moment, now=now) == "1h ago"


def test_equivalent_offsets_have_no_age() -> None:
    assert relative_age(datetime.fromisoformat("2026-01-10T20:00:00+08:00"), now=_NOW) == "0s ago"


@pytest.mark.parametrize("argument", ["moment", "now"])
def test_relative_age_requires_aware_values(argument: str) -> None:
    naive = _NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match=rf"{argument} must carry tzinfo"):
        relative_age(
            naive if argument == "moment" else _NOW, now=naive if argument == "now" else _NOW
        )
