from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from base.lm.harness.session import BackgroundItem, BackgroundKind, describe

# meiva:note
# What the stdout step stream lets this adapter know about the `schedule` tool (cron jobs and
# timers). Recorded on agy 1.5.5: a timer call (`{DurationSeconds, Prompt}`) blocks inside the turn
# and its result arrives when the timer fires, so it needs no item after the turn: the turn itself
# is the work while it blocks. A call that names no duration (a cron job) shows neither its firing
# nor a cancellation, so a successful one counts as pending for a day, which the host's cache bound
# still caps; a job the stream never shows (one set before this process started) is not seen.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
SCHEDULE_TOOL = "schedule"
UNBOUNDED_SCHEDULE_SECONDS = 24 * 3600.0
AFTER_DUE_MARGIN_SECONDS = 180.0
_LIMIT = 100


@dataclass(frozen=True, slots=True)
class Schedules:
    calls: tuple[tuple[str, Mapping[str, object]], ...] = ()
    items: tuple[BackgroundItem, ...] = ()

    def tool_used(self, tool_use_id: str, name: str, params: Mapping[str, object]) -> Schedules:
        if name != SCHEDULE_TOOL:
            return self
        return Schedules((*self.calls, (tool_use_id, params)), self.items)

    def tool_returned(self, tool_use_id: str, *, is_error: bool, now: float) -> Schedules:
        call = next((entry for entry in self.calls if entry[0] == tool_use_id), None)
        if call is None:
            return self
        rest = tuple(entry for entry in self.calls if entry[0] != tool_use_id)
        if is_error:
            return Schedules(rest, self.items)
        params = call[1]
        wait = _duration(params)
        if wait is not None:
            return Schedules(rest, self.items)
        until = now + (
            UNBOUNDED_SCHEDULE_SECONDS if wait is None else wait + AFTER_DUE_MARGIN_SECONDS
        )
        label = describe(json.dumps(dict(params), sort_keys=True, default=str), limit=_LIMIT)
        item = BackgroundItem(BackgroundKind.SCHEDULE, f"schedule {label}", until=until)
        return Schedules(rest, (*self.items, item))

    def pending_at(self, now: float) -> tuple[BackgroundItem, ...]:
        return tuple(item for item in self.items if item.pending_at(now))


def _duration(params: Mapping[str, object]) -> float | None:
    for key, value in params.items():
        if key.lower() != "durationseconds":
            continue
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str) and value.strip().isdigit():
            return float(value.strip())
    return None
