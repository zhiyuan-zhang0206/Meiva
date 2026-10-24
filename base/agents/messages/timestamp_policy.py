# meiva:note
# Whether messages carry a timestamp, and how one renders, as one pure value.
#
# The history renderers, the inbound envelope and the takeover notes each stamp messages with the
# same facts: the `general.message_timestamps` switch, the cluster timezone, whether the weekday
# shows, and the instant of the unit of work that renders. A composition root mints the value for
# the unit of work it starts (a node, a settle, a request) and hands it down; nothing below it
# reads Settings or builds a clock. A root that outlives one unit of work holds a
# `TimestampSource` and mints a fresh value from it per use, so a long-lived holder never serves a
# stale instant.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from base.clock import Clock, format_message_timestamp

__all__ = ["TimestampPolicy", "TimestampSource"]


# meiva:note
# `timestamps_enabled` is the display switch; `timezone` and `weekday` shape every stamp; `now` is
# the instant of the unit of work. `now` is None only for a policy that stamps nothing and notes
# no time (`off()`); a stamp or a note time asked of a policy without one raises.
@dataclass(frozen=True)
class TimestampPolicy:
    timestamps_enabled: bool
    timezone: str
    weekday: bool
    now: datetime | None

    @classmethod
    def from_clock(cls, clock: Clock, *, timestamps_enabled: bool) -> TimestampPolicy:
        return cls(
            timestamps_enabled=timestamps_enabled,
            timezone=clock.timezone,
            weekday=clock.message_timestamp_weekday,
            now=clock.now(),
        )

    @classmethod
    def off(cls) -> TimestampPolicy:
        return cls(timestamps_enabled=False, timezone="UTC", weekday=False, now=None)

    def require_now(self) -> datetime:
        if self.now is None:
            raise RuntimeError("this timestamp policy carries no instant")
        return self.now

    def format(self, moment: datetime) -> str:
        return format_message_timestamp(moment, timezone=self.timezone, weekday=self.weekday)

    def now_stamp(self) -> str:
        return self.format(self.require_now())


# meiva:note
# What a long-lived root keeps in place of a policy: the clock and the switch, which mint a
# policy for the instant of each use.
@dataclass(frozen=True)
class TimestampSource:
    clock: Clock
    timestamps_enabled: bool

    def policy(self) -> TimestampPolicy:
        return TimestampPolicy.from_clock(self.clock, timestamps_enabled=self.timestamps_enabled)
