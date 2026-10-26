# meiva:link base/clock/docs/clock.meiva.okf.md
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class ClockConfig:
    timezone: str
    authoritative_timezone: str | None
    message_timestamp_weekday: bool


def clock_config_from_settings() -> ClockConfig:
    from base.config import cluster_tz_name, settings

    return ClockConfig(
        timezone=settings.general.timezone,
        authoritative_timezone=cluster_tz_name(),
        message_timestamp_weekday=settings.general.message_timestamp_weekday,
    )


class Timers(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    def asleep(self, seconds: float) -> Coroutine[object, object, None]: ...


class _SystemTimers:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def asleep(self, seconds: float) -> Coroutine[object, object, None]:
        return asyncio.sleep(seconds)


SYSTEM_TIMERS: Timers = _SystemTimers()


def _utc_now() -> datetime:
    return datetime.now(UTC)


# meiva:note
# The bracketed message timestamp of `moment` in the IANA zone `timezone`, with the weekday when
# `weekday`. A zone name that names no zone raises `ZoneInfoNotFoundError`.
def format_message_timestamp(moment: datetime, *, timezone: str, weekday: bool) -> str:
    local = moment.astimezone(ZoneInfo(timezone))
    if weekday:
        return local.strftime("[%Y-%m-%d %a %H:%M:%S]")
    return local.strftime("[%Y-%m-%d %H:%M:%S]")


class Clock:
    def __init__(
        self,
        config: ClockConfig,
        *,
        timers: Timers,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._timers = timers
        self._now = now

    @classmethod
    def from_settings(cls) -> Clock:
        return cls(clock_config_from_settings(), timers=SYSTEM_TIMERS)

    @property
    def timezone(self) -> str:
        return self._config.timezone

    @property
    def authoritative_timezone(self) -> str | None:
        return self._config.authoritative_timezone

    def now(self) -> datetime:
        return self._now()

    def zone(self) -> ZoneInfo | None:
        name = self._config.authoritative_timezone
        if name is None:
            return None
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return None

    def explicit_zone(self) -> ZoneInfo:
        return ZoneInfo(self._config.timezone)

    @property
    def message_timestamp_weekday(self) -> bool:
        return self._config.message_timestamp_weekday

    def format_timestamp(self, dt: datetime) -> str:
        return format_message_timestamp(
            dt, timezone=self._config.timezone, weekday=self._config.message_timestamp_weekday
        )

    def now_timestamp(self) -> str:
        return self.format_timestamp(self.now())

    def monotonic(self) -> float:
        return self._timers.monotonic()

    def sleep(self, seconds: float) -> None:
        self._timers.sleep(seconds)

    def asleep(self, seconds: float) -> Coroutine[object, object, None]:
        return self._timers.asleep(seconds)
