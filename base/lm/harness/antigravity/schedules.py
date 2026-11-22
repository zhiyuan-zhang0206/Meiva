from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace

from base.lm.harness.session import BackgroundItem, BackgroundKind, describe

# meiva:note
# What the stdout step stream lets this adapter know about the `schedule` tool (cron jobs and
# timers) and `manage_task` cancellations. Recorded on agy 1.5.5: a timer call
# (`{DurationSeconds, Prompt}`) blocked inside the tool call until it fired. Recorded on agy
# 1.5.14: a timer call returns immediately as a background task handle (`task id: ...`) while
# holding the outer turn open and silent until the timer fires or is cancelled early by
# `TimerCondition` / `manage_task(Action="kill")`. Both forms keep their work inside the open
# turn (so `pending_at` adds no post-turn item for timed calls), while `timers` tracks the
# per-turn monotonic silence floor so the foreground stall watchdog does not kill a turn
# legitimately waiting on a declared timer. A cron call (no duration) shows neither its firing
# nor a cancellation, so a successful one counts as pending for a day (`UNBOUNDED_SCHEDULE_SECONDS`),
# which the host's cache bound still caps.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
SCHEDULE_TOOL = "schedule"
MANAGE_TASK_TOOL = "manage_task"
UNBOUNDED_SCHEDULE_SECONDS = 24 * 3600.0
_LIMIT = 100
_TASK_ID_RE = re.compile(r"task id:\s*([^\s\"']+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ActiveTimer:
    turn_id: str
    tool_use_id: str
    due_monotonic: float
    conditional: bool = False
    task_id: str | None = None
    armed: bool = False
    waited: bool = False


@dataclass(frozen=True, slots=True)
class _PendingKill:
    tool_use_id: str
    turn_id: str
    task_id: str | None


@dataclass(frozen=True, slots=True)
class Schedules:
    calls: tuple[tuple[str, Mapping[str, object]], ...] = ()
    items: tuple[BackgroundItem, ...] = ()
    timers: tuple[ActiveTimer, ...] = ()
    pending_kills: tuple[_PendingKill, ...] = ()
    max_timer_seconds: float = UNBOUNDED_SCHEDULE_SECONDS

    def tool_used(
        self,
        tool_use_id: str,
        name: str,
        params: Mapping[str, object],
        *,
        turn_id: str = "",
        monotonic_now: float | None = None,
    ) -> Schedules:
        if name == SCHEDULE_TOOL:
            timers = self.timers
            if monotonic_now is not None and (wait := _duration(params)) is not None:
                capped = min(wait, self.max_timer_seconds)
                timers = (
                    *timers,
                    ActiveTimer(
                        turn_id=turn_id,
                        tool_use_id=tool_use_id,
                        due_monotonic=monotonic_now + capped,
                        conditional=_is_conditional(params),
                    ),
                )
            return replace(self, calls=(*self.calls, (tool_use_id, params)), timers=timers)
        if name == MANAGE_TASK_TOOL and _is_kill_action(params):
            kill = _PendingKill(
                tool_use_id=tool_use_id,
                turn_id=turn_id,
                task_id=_task_id_param(params),
            )
            return replace(self, pending_kills=(*self.pending_kills, kill))
        if monotonic_now is None or not self.timers:
            return self
        return self._prune_on_activity(
            turn_id, monotonic_now=monotonic_now, drop_armed_conditional=True
        )

    def tool_returned(
        self,
        tool_use_id: str,
        *,
        is_error: bool,
        now: float,
        content: str = "",
        monotonic_now: float | None = None,
    ) -> Schedules:
        kill = next(
            (entry for entry in self.pending_kills if entry.tool_use_id == tool_use_id), None
        )
        if kill is not None:
            return self._finish_kill(kill, is_error=is_error)
        call = next((entry for entry in self.calls if entry[0] == tool_use_id), None)
        if call is None:
            return self
        rest = tuple(entry for entry in self.calls if entry[0] != tool_use_id)
        if is_error:
            timers = tuple(t for t in self.timers if t.tool_use_id != tool_use_id)
            return replace(self, calls=rest, timers=timers)
        params = call[1]
        wait = _duration(params)
        if wait is not None:
            timers = _arm_timer(
                self.timers,
                tool_use_id=tool_use_id,
                task_id=_extract_task_id(content),
                monotonic_now=monotonic_now,
            )
            return replace(self, calls=rest, timers=timers)
        until = now + self.max_timer_seconds
        label = describe(json.dumps(dict(params), sort_keys=True, default=str), limit=_LIMIT)
        item = BackgroundItem(BackgroundKind.SCHEDULE, f"schedule {label}", until=until)
        return replace(self, calls=rest, items=(*self.items, item))

    def on_assistant_text(self, turn_id: str, *, monotonic_now: float) -> Schedules:
        if not self.timers:
            return self
        updated: list[ActiveTimer] = []
        for timer in self.timers:
            if timer.turn_id != turn_id:
                updated.append(timer)
                continue
            if timer.armed and monotonic_now >= timer.due_monotonic:
                continue
            if timer.armed and timer.conditional and timer.waited:
                continue
            updated.append(replace(timer, waited=True) if timer.armed else timer)
        return replace(self, timers=tuple(updated))

    def on_input(self, turn_id: str, *, monotonic_now: float) -> Schedules:
        return self._prune_on_activity(
            turn_id, monotonic_now=monotonic_now, drop_any_conditional=True
        )

    def on_wakeup(self, turn_id: str, *, monotonic_now: float) -> Schedules:
        return self._prune_on_activity(
            turn_id, monotonic_now=monotonic_now, drop_any_conditional=True
        )

    def clear_turn(self, turn_id: str) -> Schedules:
        timers = tuple(t for t in self.timers if t.turn_id != turn_id)
        kills = tuple(k for k in self.pending_kills if k.turn_id != turn_id)
        if timers == self.timers and kills == self.pending_kills:
            return self
        return replace(self, timers=timers, pending_kills=kills)

    def silence_floor_monotonic(self, turn_ids: Collection[str]) -> float | None:
        if not turn_ids or not self.timers:
            return None
        floors: list[float] = []
        for turn_id in turn_ids:
            turn_timers = [t.due_monotonic for t in self.timers if t.turn_id == turn_id]
            if not turn_timers:
                return None
            floors.append(max(turn_timers))
        return min(floors)

    def pending_at(self, now: float) -> tuple[BackgroundItem, ...]:
        return tuple(item for item in self.items if item.pending_at(now))

    def _finish_kill(self, kill: _PendingKill, *, is_error: bool) -> Schedules:
        rest_kills = tuple(k for k in self.pending_kills if k.tool_use_id != kill.tool_use_id)
        if is_error:
            return replace(self, pending_kills=rest_kills)
        timers = tuple(t for t in self.timers if not _kill_matches(t, kill))
        return replace(self, pending_kills=rest_kills, timers=timers)

    def _prune_on_activity(
        self,
        turn_id: str,
        *,
        monotonic_now: float,
        drop_armed_conditional: bool = False,
        drop_any_conditional: bool = False,
    ) -> Schedules:
        if not self.timers:
            return self
        kept: list[ActiveTimer] = []
        for timer in self.timers:
            if timer.turn_id != turn_id:
                kept.append(timer)
                continue
            if timer.armed and monotonic_now >= timer.due_monotonic:
                continue
            if timer.conditional and (
                drop_any_conditional or (drop_armed_conditional and timer.armed)
            ):
                continue
            kept.append(timer)
        if len(kept) == len(self.timers):
            return self
        return replace(self, timers=tuple(kept))


def _arm_timer(
    timers: tuple[ActiveTimer, ...],
    *,
    tool_use_id: str,
    task_id: str | None,
    monotonic_now: float | None,
) -> tuple[ActiveTimer, ...]:
    updated: list[ActiveTimer] = []
    for timer in timers:
        if timer.tool_use_id != tool_use_id:
            updated.append(timer)
            continue
        if monotonic_now is not None and monotonic_now >= timer.due_monotonic:
            continue
        updated.append(replace(timer, armed=True, task_id=task_id))
    return tuple(updated)


def _kill_matches(timer: ActiveTimer, kill: _PendingKill) -> bool:
    if kill.task_id is not None and timer.task_id is not None:
        return timer.task_id == kill.task_id
    return timer.turn_id == kill.turn_id and (kill.task_id is None or timer.task_id is None)


def _duration(params: Mapping[str, object]) -> float | None:
    for key, value in params.items():
        if key.lower() != "durationseconds":
            continue
        if isinstance(value, bool):
            return None
        parsed: float | None = None
        if isinstance(value, int | float):
            parsed = float(value)
        elif isinstance(value, str):
            stripped = value.strip()
            if stripped:
                try:
                    parsed = float(stripped)
                except ValueError:
                    return None
        if parsed is not None and math.isfinite(parsed) and parsed > 0:
            return parsed
        return None
    return None


def _is_conditional(params: Mapping[str, object]) -> bool:
    for key, value in params.items():
        if key.lower() != "timercondition":
            continue
        if isinstance(value, str):
            normalized = value.strip().lower()
            return bool(normalized) and normalized != "never"
        return value is not None
    return False


def _is_kill_action(params: Mapping[str, object]) -> bool:
    for key, value in params.items():
        if key.lower() == "action":
            return isinstance(value, str) and value.strip().lower() == "kill"
    return False


def _task_id_param(params: Mapping[str, object]) -> str | None:
    for key, value in params.items():
        if key.lower() == "taskid" and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_task_id(content: str) -> str | None:
    match = _TASK_ID_RE.search(content)
    return match.group(1) if match else None
