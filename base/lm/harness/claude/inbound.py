from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from base.lm.harness.events import (
    AssistantText,
    BridgeEvent,
    SessionStarted,
    ToolUse,
    UnsolicitedOutput,
)


# meiva:note
# What one inbound SDK message means to the session, read off the name of the class the
# injected SDK emitted: a result is a turn boundary, the three task messages are reports
# about work outside the turn, the session messages are about the session, and everything
# else arrived inside the running turn. Dispatch is by class name rather than `isinstance`
# because the SDK is injected and a test fake carries the same class names and fields; a
# name nobody knows is inside the turn, which is how a newer SDK stays compatible.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
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
        case "RateLimitEvent" | "SystemMessage":
            return MessageKind.SESSION
        case _:
            return MessageKind.TURN


# meiva:note
# `human` is the origin of a turn this session started by submitting a prompt; every other
# origin names a turn the CLI began on its own, which this session never started, so it comes
# back as no claim at all and the result is recorded as unsolicited output (#5663).
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
HUMAN_ORIGIN = "human"


def claimed_origin(origin_kind: str | None) -> str | None:
    return None if origin_kind is None or origin_kind == HUMAN_ORIGIN else origin_kind


# meiva:note
# A step is one assistant message that produced text or a tool call: the CLI reports one
# assistant message per model step, and a turn's step count floors the `num_turns` its result
# reports, so a turn that ran tools is never read as a single step.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
def counts_as_step(class_name: str, events: Sequence[BridgeEvent]) -> bool:
    return class_name == "AssistantMessage" and any(
        isinstance(event, (AssistantText, ToolUse)) for event in events
    )


# meiva:note
# What a turn's own text is: the assistant's text blocks, in the order they streamed. A
# reasoning block is not the reply, and a tool result the CLI echoes back is not either.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
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
# meiva:link base/lm/harness/claude/docs/background-tasks.meiva.okf.md
TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped", "killed"})


@dataclass(frozen=True, slots=True)
class BackgroundTasks:
    task_ids: frozenset[str] = frozenset()

    @property
    def open_task_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.task_ids))

    def started(self, task_id: str) -> BackgroundTasks:
        if not task_id:
            return self
        return BackgroundTasks(self.task_ids | {task_id})

    def reported(self, task_id: str) -> BackgroundTasks:
        return BackgroundTasks(self.task_ids - {task_id})

    def updated(self, task_id: str, status: str | None) -> BackgroundTasks:
        if status not in TERMINAL_TASK_STATUSES:
            return self
        return self.reported(task_id)


# meiva:note
# What one task notification delivers. Two different things arrive under one message: the
# report the model should read, and — whenever the CLI answered it itself — a turn of the
# CLI's own that streams right behind it. While a turn of ours is running, the CLI put the
# report in that turn's own context, so a second delivery would be a duplicate turn and the
# report stays in the stream; with no turn running nobody has read it, so the sink takes it
# and dispatch is held until the CLI's own answer reports. A task notification maps to
# exactly one report, which is why splitting the two does not reorder the stream (#5663).
# meiva:link base/lm/harness/claude/docs/background-tasks.meiva.okf.md
@dataclass(frozen=True, slots=True)
class NotificationPlan:
    stream: tuple[BridgeEvent, ...]
    report: UnsolicitedOutput | None


def notification_plan(events: Sequence[BridgeEvent], *, claimed: bool) -> NotificationPlan:
    report = next((event for event in events if isinstance(event, UnsolicitedOutput)), None)
    if claimed or report is None:
        return NotificationPlan(stream=tuple(events), report=None)
    return NotificationPlan(
        stream=tuple(event for event in events if event is not report), report=report
    )


# meiva:note
# What the CLI's init says about the session it opened, with the fallbacks the stream and the
# registry read: a message that names no session id or model keeps the one the session already
# has, so a CLI that reports only part of it cannot blank the identity pair.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
@dataclass(frozen=True, slots=True)
class SessionIdentity:
    session_id: str | None
    model: str | None


def started_identity(
    event: SessionStarted, *, session_id: str | None, model: str | None
) -> SessionIdentity:
    return SessionIdentity(session_id=event.session_id or session_id, model=event.model or model)
