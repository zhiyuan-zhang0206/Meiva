# meiva:note
# Appointment timing and the anti-wake outcome routing matrix.

from datetime import UTC, datetime, timedelta

import pytest

from gateway.agents.launch.scheduled_lifecycle import outcome_recipients, parse_at

NOW = datetime(2026, 11, 17, tzinfo=UTC)


@pytest.mark.parametrize("spec,seconds", [("+60s", 60), ("+10m", 600)])
def test_relative_time_uses_schedule_parser(spec: str, seconds: int) -> None:
    assert parse_at(spec, now=NOW) == NOW + timedelta(seconds=seconds)


def test_iso_and_invalid_times() -> None:
    assert parse_at("2026-11-18T00:00:00Z", now=NOW) == NOW + timedelta(days=1)
    for spec in ("+0s", "+-1s", "2026-11-16T00:00:00Z", "2026-11-18T00:00:00", "tomorrow"):
        with pytest.raises((ValueError, TypeError)):
            parse_at(spec, now=NOW)


@pytest.mark.parametrize(
    "source,failed,running,expected",
    [
        ("agent:1", False, False, ()),
        ("agent:1", True, False, (1,)),
        ("agent:1", True, True, (1,)),
        ("agent:2", False, False, (2,)),
        ("agent:2", True, False, (2,)),
        ("agent:2", True, True, (2, 1)),
        ("user", False, False, ()),
        ("user", True, False, ()),
        ("user", True, True, (1,)),
    ],
)
def test_outcome_matrix(
    source: str, failed: bool, running: bool, expected: tuple[int, ...]
) -> None:
    assert outcome_recipients(1, source, failed=failed, running=running) == expected
