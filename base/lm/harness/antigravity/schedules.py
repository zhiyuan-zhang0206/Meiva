from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace

from base.lm.harness.session import BackgroundItem, BackgroundKind, describe

# meiva:note
# ACP reports timers that outlive their foreground prompt as still-running schedule calls.
# Active timers supply the foreground watchdog's silence floor. Once their turn ends, the
# unresolved calls keep the process alive through the delayed reply.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
SCHEDULE_TOOL = "schedule"
MANAGE_TASK_TOOL = "manage_task"
UNBOUNDED_SCHEDULE_SECONDS = 24 * 3600.0
BACKGROUND_REPLY_SECONDS = 30.0
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
class _DetachedTimer:
    tool_id: str
    due: float
    work: BackgroundItem
    prompt: str
    task_id: str | None


@dataclass(frozen=True, slots=True)
class Schedules:
    calls: tuple[tuple[str, Mapping[str, object]], ...] = ()
    items: tuple[BackgroundItem, ...] = ()
    timers: tuple[ActiveTimer, ...] = ()
    background_timers: tuple[_DetachedTimer, ...] = ()
    replies: tuple[_DetachedTimer, ...] = ()
    reply_wait_seconds: float = BACKGROUND_REPLY_SECONDS
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
        calls = dict(self.calls)
        params = calls.pop(tool_use_id, None)
        if params is None:
            return self
        remaining = self._finish_detached(tool_use_id, now=now, is_error=is_error)
        remaining = replace(remaining, calls=tuple(calls.items()))
        if is_error:
            return replace(
                remaining, timers=tuple(t for t in self.timers if t.tool_use_id != tool_use_id)
            )
        if _duration(params) is not None:
            timers = _arm_timer(
                self.timers,
                tool_use_id=tool_use_id,
                task_id=_extract_task_id(content),
                monotonic_now=monotonic_now,
            )
            return replace(remaining, timers=timers)
        until = now + self.max_timer_seconds
        item = BackgroundItem(BackgroundKind.SCHEDULE, _schedule_label(params), until=until)
        return replace(remaining, items=(*self.items, item))

    def _finish_detached(self, tool_id: str, *, now: float, is_error: bool) -> Schedules:
        pending = tuple(timer for timer in self.background_timers if timer.tool_id != tool_id)
        if is_error or len(pending) == len(self.background_timers):
            return replace(self, background_timers=pending)
        replies = (
            replace(timer, work=replace(timer.work, until=now + self.reply_wait_seconds))
            for timer in self.background_timers
            if timer.tool_id == tool_id
        )
        return replace(self, background_timers=pending, replies=(*self.replies, *replies))

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

    def finish_turn(
        self, turn_id: str, *, now: float, monotonic_now: float, cancelled: bool = False
    ) -> Schedules:
        calls = dict(self.calls)
        current = tuple(t for t in self.timers if t.turn_id == turn_id)
        detached = (
            ()
            if cancelled
            else tuple(
                self._detach(
                    timer, calls[timer.tool_use_id], due=now + timer.due_monotonic - monotonic_now
                )
                for timer in current
                if timer.tool_use_id in calls
            )
        )
        return replace(
            self,
            timers=tuple(t for t in self.timers if t.turn_id != turn_id),
            background_timers=(*self.background_timers, *detached),
            pending_kills=tuple(k for k in self.pending_kills if k.turn_id != turn_id),
        )

    def _detach(
        self, timer: ActiveTimer, params: Mapping[str, object], *, due: float
    ) -> _DetachedTimer:
        work = BackgroundItem(
            BackgroundKind.SCHEDULE, _schedule_label(params), until=due + self.reply_wait_seconds
        )
        prompt = next((value for key, value in params.items() if key.lower() == "prompt"), None)
        return _DetachedTimer(
            timer.tool_use_id,
            due,
            work,
            prompt.strip() if isinstance(prompt, str) else "",
            timer.task_id,
        )

    @property
    def has_detached(self) -> bool:
        return bool(self.background_timers or self.replies)

    def reply_deadline(self, now: float) -> float | None:
        due = tuple(timer.work.until for timer in self.background_timers if timer.due <= now)
        deadlines = (*due, *(reply.work.until for reply in self.replies))
        return min((end for end in deadlines if end is not None), default=None)

    def reply_finished(self, sender: str, prompt: str) -> Schedules:
        for index, reply in enumerate(self.replies):
            if reply.task_id == sender or (reply.task_id is None and reply.prompt == prompt):
                return replace(self, replies=(*self.replies[:index], *self.replies[index + 1 :]))
        return self

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
        items = (*self.items, *(timer.work for timer in (*self.replies, *self.background_timers)))
        return tuple(item for item in items if item.pending_at(now))

    def has_active_wait(self, turn_id: str, *, now: float, monotonic_now: float) -> bool:
        return any(
            t.turn_id == turn_id and monotonic_now < t.due_monotonic for t in self.timers
        ) or bool(self.pending_at(now))

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


def _schedule_label(params: Mapping[str, object]) -> str:
    label = describe(json.dumps(dict(params), sort_keys=True, default=str), limit=_LIMIT)
    return f"schedule {label}"


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
