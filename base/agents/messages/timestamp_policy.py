# meiva:note
# Whether messages carry a timestamp, and the clock that renders it, as one value.
#
# The history renderers, the inbound envelope and the takeover notes each stamp messages with the
# same two facts: the cluster clock and the `general.message_timestamps` switch. A composition
# root reads both once for the unit of work it starts (a node, a settle, a request) and hands
# the value down; nothing below it reads Settings or builds a clock.

from __future__ import annotations

from dataclasses import dataclass

from base.clock import Clock

__all__ = ["TimestampPolicy"]


# meiva:note
# `clock` renders every stamp of the unit of work; `timestamps_enabled` is the display switch.
@dataclass(frozen=True)
class TimestampPolicy:
    clock: Clock
    timestamps_enabled: bool
