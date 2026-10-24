from __future__ import annotations

from datetime import datetime
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


# meiva:note
# A real Clock with a refused wall-time observation, for admission paths that must not ask it.
# The failure is fixed at construction; no mutable arming flag or process-global patch is needed.
def clock_rejecting_now() -> Clock:
    def rejected() -> datetime:
        raise AssertionError("this operation must not observe wall time")

    return Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=rejected)
