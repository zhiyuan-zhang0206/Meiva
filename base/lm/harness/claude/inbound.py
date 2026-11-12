from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.events import (
    AssistantText,
    BridgeEvent,
    SessionStarted,
    ToolUse,
)
from base.lm.harness.session import BackgroundItem, BackgroundKind, describe


# meiva:note
# What one inbound SDK message means to the session, read off the name of the class the
# injected SDK emitted: a result is a turn boundary, the three task messages are reports
# about work outside the turn, the session messages are about the session, and everything
# else arrived inside the running turn. Dispatch is by class name rather than `isinstance`
# because the SDK is injected and a test fake carries the same class names and fields; a
# name nobody knows is inside the turn, which is how a newer SDK stays compatible.
# meiva:link base/lm/harness/claude/docs/claude.md
class MessageKind(StrEnum):
    RESULT = "result"
    TASK_NOTIFICATION = "task-notification"
    TASK_STARTED = "task-started"
    TASK_UPDATED = "task-updated"
    SESSION = "session"
    TURN = "turn"


def message_kind(class_name: str) -> MessageKind:
    match class_name:
        case "ResultMessage":
            return MessageKind.RESULT
        case "TaskNotificationMessage":
            return MessageKind.TASK_NOTIFICATION
        case "TaskStartedMessage":
            return MessageKind.TASK_STARTED
        case "TaskUpdatedMessage":
            return MessageKind.TASK_UPDATED
        # meiva:note
        # A hook report (a SessionStart hook in the user's settings runs before the CLI echoes the first
        # input) is about the session, never content of a turn: read as turn content it opened a turn
        # that no input and no CLI behaviour had begun, and the real turn then joined it.
        # meiva:link base/lm/harness/claude/docs/claude.md
        case "RateLimitEvent" | "SystemMessage" | "HookEventMessage":
            return MessageKind.SESSION
        case _:
            return MessageKind.TURN


# meiva:note
# A user message is either the result of a tool the model called (content of the running turn) or the
# CLI's echo of an input that entered the conversation. Only the first has `ToolResultBlock`s.
# meiva:link base/lm/harness/claude/docs/claude.md
def is_tool_result(message: object) -> bool:
    content: object = getattr(message, "content", None)
    return is_object_list(content) and any(
        type(block).__name__ == "ToolResultBlock" for block in content
    )


# meiva:note
# A step is one assistant message that produced text or a tool call: the CLI reports one
# assistant message per model step, and a turn's step count floors the `num_turns` its result
# reports, so a turn that ran tools is never read as a single step.
# meiva:link base/lm/harness/claude/docs/claude.md
def counts_as_step(class_name: str, events: Sequence[BridgeEvent]) -> bool:
    return class_name == "AssistantMessage" and any(
        isinstance(event, (AssistantText, ToolUse)) for event in events
    )


# meiva:note
# What a turn's own text is: the assistant's text blocks, in the order they streamed. A
# reasoning block is not the reply, and a tool result the CLI echoes back is not either.
# meiva:link base/lm/harness/claude/docs/claude.md
def assistant_texts(events: Sequence[BridgeEvent]) -> tuple[str, ...]:
    return tuple(event.text for event in events if isinstance(event, AssistantText))


# meiva:note
# The tasks the CLI says are running: started and not yet reported terminal. A task's
# terminal state can arrive only as an update, with no notification — a task stopped through
# the control request reports `killed` and the notification that would have followed is
# sometimes suppressed — so both messages clear it. Clearing is what keeps `close()` from
# asking the CLI to stop work that already ended. These four values are the SDK's own
# `TERMINAL_TASK_STATUSES`, spelled out here so the adapter's fake carries the same rule the
# real CLI follows and a bump of the SDK constant shows up as a diff in this file.
# meiva:link base/lm/harness/claude/docs/background-tasks.md
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped", "killed"})


@dataclass(frozen=True, slots=True)
class BackgroundTasks:
    task_ids: frozenset[str] = frozenset()
    # meiva:note
    # What each open task is, for the note that tells the agent it was lost: the provider's own
    # one-line description, and whether it is a sub-agent or a command.
    labels: tuple[tuple[str, BackgroundItem], ...] = ()

    @property
    def open_task_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.task_ids))

    @property
    def items(self) -> tuple[BackgroundItem, ...]:
        by_id = dict(self.labels)
        return tuple(
            by_id.get(task_id) or BackgroundItem(BackgroundKind.COMMAND, f"task {task_id}")
            for task_id in self.open_task_ids
        )

    def started(
        self, task_id: str, *, description: str = "", task_type: str = ""
    ) -> BackgroundTasks:
        if not task_id:
            return self
        kind = BackgroundKind.AGENT if "agent" in task_type else BackgroundKind.COMMAND
        label = BackgroundItem(kind, describe(description) or f"task {task_id}")
        return BackgroundTasks(
            self.task_ids | {task_id},
            (*(entry for entry in self.labels if entry[0] != task_id), (task_id, label)),
        )

    def reported(self, task_id: str) -> BackgroundTasks:
        return BackgroundTasks(
            self.task_ids - {task_id},
            tuple(entry for entry in self.labels if entry[0] != task_id),
        )

    def updated(self, task_id: str, status: str | None) -> BackgroundTasks:
        if status not in TERMINAL_TASK_STATUSES:
            return self
        return self.reported(task_id)


# meiva:note
# What the CLI's init says about the session it opened, with the fallbacks the stream and the
# registry read: a message that names no session id or model keeps the one the session already
# has, so a CLI that reports only part of it cannot blank the identity pair.
# meiva:link base/lm/harness/claude/docs/claude.md
@dataclass(frozen=True, slots=True)
class SessionIdentity:
    session_id: str | None
    model: str | None


def started_identity(
    event: SessionStarted, *, session_id: str | None, model: str | None
) -> SessionIdentity:
    return SessionIdentity(session_id=event.session_id or session_id, model=event.model or model)


# meiva:note
# The timers a Claude model sets with its own tools, followed from the tool calls and their results in
# the event stream. The CLI keeps them in memory and fires them as turns of its own while the process
# lives; they die with it (`durable` has no effect: "gone when this Claude session ends"). Nothing in
# the stream says a timer fired, so the clock decides the end: a `ScheduleWakeup` is due at the moment
# its result names (else `delaySeconds` from the result text) and a one-shot cron at the `nextFireAt`
# its result carries; both stop counting a little after that. A recurring cron lasts until
# `CronDelete` and is dropped after the CLI's own seven-day expiry. `CronList` is the CLI's own
# answer to "which jobs exist" and replaces the cron set when its result carries the structured list.
# The result shapes are the ones recorded from the CLI (CronCreate, CronDelete, CronList,
# ScheduleWakeup); a shape this does not recognise keeps the call counted as pending, because a timer
# wrongly forgotten is silent and one wrongly kept only costs a process until the cache bound.
# meiva:link base/lm/harness/claude/docs/background-tasks.md
CRON_EXPIRY_SECONDS = 7 * 24 * 3600.0
AFTER_DUE_MARGIN_SECONDS = 180.0
_SCHEDULING_TOOLS = frozenset({"CronCreate", "CronDelete", "CronList", "ScheduleWakeup"})
_CRON_ID = re.compile(r"Scheduled (?:recurring job|one-shot task) (\w+)")
_WAKEUP_DELAY = re.compile(r"\(in (\d+)s\)")
_PROMPT_LIMIT = 80


@dataclass(frozen=True, slots=True)
class ScheduledWork:
    calls: tuple[tuple[str, str, Mapping[str, object]], ...] = ()
    jobs: tuple[tuple[str, BackgroundItem], ...] = ()

    def tool_used(
        self, tool_use_id: str, name: str, tool_input: Mapping[str, object]
    ) -> ScheduledWork:
        if name not in _SCHEDULING_TOOLS:
            return self
        return ScheduledWork((*self.calls, (tool_use_id, name, tool_input)), self.jobs)

    def tool_returned(
        self,
        tool_use_id: str,
        *,
        content: str,
        is_error: bool,
        structured: Mapping[str, object] | None,
        now: float,
        fire_fallback: float | None = None,
    ) -> ScheduledWork:
        call = next((entry for entry in self.calls if entry[0] == tool_use_id), None)
        if call is None:
            return self
        _, name, tool_input = call
        rest = ScheduledWork(tuple(c for c in self.calls if c[0] != tool_use_id), self.jobs)
        if is_error:
            return rest
        result = structured or {}
        if name == "CronCreate":
            return rest._created(tool_use_id, tool_input, content, result, now, fire_fallback)
        if name == "CronDelete":
            gone = tool_input.get("id")
            return rest._without(gone) if isinstance(gone, str) else rest
        if name == "CronList":
            return rest._listed(result, now)
        if tool_input.get("stop") is True:
            return rest._without_wakeups()
        return rest._woken(tool_use_id, tool_input, content, result, now)

    # meiva:note
    # The input of a pending call, so the shell can date a one-shot cron whose result carried no fire
    # time from the cron expression (the shell reads the local zone; this module stays pure).
    def call_input(self, tool_use_id: str, name: str) -> Mapping[str, object] | None:
        found = next((c for c in self.calls if c[0] == tool_use_id and c[1] == name), None)
        return None if found is None else found[2]

    def pending_at(self, now: float) -> tuple[BackgroundItem, ...]:
        return tuple(item for _, item in self.jobs if item.pending_at(now))

    # meiva:note
    # A turn the CLI began with no echo and no task notification, while a wakeup or one-shot cron has
    # come due, is that timer firing: the stream carries no echo of a fired prompt and no origin on
    # its result (recorded on CLI 2.1.290), so the due item is the only evidence. The fired items are
    # removed and the caller names the cause; a recurring cron that is pending also explains such a
    # turn but stays.
    # meiva:link base/lm/harness/claude/docs/background-tasks.md
    def fired(self, now: float) -> tuple[ScheduledWork, bool]:
        gone = tuple(job for job in self.jobs if _is_due(job[1], now))
        recurring = any(
            item.kind is BackgroundKind.CRON and item.description.startswith("recurring")
            for _, item in self.jobs
            if item.pending_at(now)
        )
        if not gone:
            return self, recurring
        return ScheduledWork(self.calls, tuple(j for j in self.jobs if j not in gone)), True

    def _without(self, job_id: str) -> ScheduledWork:
        return ScheduledWork(self.calls, tuple(job for job in self.jobs if job[0] != job_id))

    def _without_wakeups(self) -> ScheduledWork:
        return ScheduledWork(
            self.calls,
            tuple(job for job in self.jobs if job[1].kind is not BackgroundKind.WAKEUP),
        )

    def _with(self, key: str, item: BackgroundItem) -> ScheduledWork:
        return ScheduledWork(
            self.calls, (*(job for job in self.jobs if job[0] != key), (key, item))
        )

    def _created(
        self,
        tool_use_id: str,
        tool_input: Mapping[str, object],
        content: str,
        result: Mapping[str, object],
        now: float,
        fire_fallback: float | None,
    ) -> ScheduledWork:
        job_id = result.get("id")
        if not isinstance(job_id, str):
            found = _CRON_ID.search(content)
            job_id = found.group(1) if found is not None else f"call:{tool_use_id}"
        recurring = tool_input.get("recurring", True) is not False
        fires = _seconds(result.get("nextFireAt"))
        if fires is None and not recurring:
            fires = fire_fallback
        until = (
            now + CRON_EXPIRY_SECONDS
            if recurring or fires is None
            else fires + AFTER_DUE_MARGIN_SECONDS
        )
        return self._with(job_id, _cron_item(tool_input, recurring=recurring, until=until))

    def _woken(
        self,
        tool_use_id: str,
        tool_input: Mapping[str, object],
        content: str,
        result: Mapping[str, object],
        now: float,
    ) -> ScheduledWork:
        due = _seconds(result.get("scheduledFor"))
        if due is None:
            delay = _WAKEUP_DELAY.search(content)
            wait = (
                float(delay.group(1))
                if delay is not None
                else _number(tool_input.get("delaySeconds"))
            )
            due = now + (wait or 0.0)
        prompt = tool_input.get("prompt")
        text = describe(prompt if isinstance(prompt, str) else "", limit=_PROMPT_LIMIT)
        item = BackgroundItem(
            BackgroundKind.WAKEUP,
            f"wakeup with the prompt: {text}" if text else "wakeup",
            until=due + AFTER_DUE_MARGIN_SECONDS,
        )
        return self._with(f"wakeup:{tool_use_id}", item)

    def _listed(self, result: Mapping[str, object], now: float) -> ScheduledWork:
        listed = result.get("jobs")
        if not is_object_list(listed):
            return self
        known = dict(self.jobs)
        crons: list[tuple[str, BackgroundItem]] = []
        for entry in listed:
            if not is_object_dict(entry) or not isinstance(entry.get("id"), str):
                return self
            job_id = str(entry["id"])
            item = known.get(job_id) or _cron_item(
                entry,
                recurring=entry.get("recurring", True) is not False,
                until=now + CRON_EXPIRY_SECONDS,
            )
            crons.append((job_id, item))
        others = tuple(job for job in self.jobs if job[1].kind is not BackgroundKind.CRON)
        return ScheduledWork(self.calls, (*others, *crons))


def _is_due(item: BackgroundItem, now: float) -> bool:
    one_shot = item.kind is BackgroundKind.WAKEUP or item.description.startswith("one-shot")
    return one_shot and item.until is not None and item.until - AFTER_DUE_MARGIN_SECONDS <= now


def _cron_item(fields: Mapping[str, object], *, recurring: bool, until: float) -> BackgroundItem:
    prompt = fields.get("prompt")
    text = describe(prompt if isinstance(prompt, str) else "", limit=_PROMPT_LIMIT)
    shape = "recurring" if recurring else "one-shot"
    return BackgroundItem(
        BackgroundKind.CRON,
        f"{shape} cron `{fields.get('cron')}` with the prompt: {text}",
        until=until,
    )


def _seconds(value: object) -> float | None:
    number = _number(value)
    return None if number is None else number / 1000.0


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
