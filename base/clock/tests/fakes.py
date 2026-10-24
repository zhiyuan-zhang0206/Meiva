from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig


def fix_zone(monkeypatch: pytest.MonkeyPatch, zone: ZoneInfo | None) -> None:

    def fixed(_clock: Clock) -> ZoneInfo | None:
        return zone

    monkeypatch.setattr(Clock, "zone", fixed)


# meiva:note
# A real clock whose timezone names no zone, so every timestamp it renders raises
# `ZoneInfoNotFoundError`: the render failure a misconfigured `general.timezone` produces.
def clock_with_unknown_timezone() -> Clock:
    config = ClockConfig(
        timezone="Not/A_Zone", authoritative_timezone=None, message_timestamp_weekday=False
    )
    return Clock(config, timers=SYSTEM_TIMERS)
