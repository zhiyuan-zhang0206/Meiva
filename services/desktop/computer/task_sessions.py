"""Task-session tracking for the computer-use daemon's audit stream (Phase 2).

`computer_action` rows already carry the caller's task_id (a caller-side
handle, not a daemon notion). This module adds the session envelope around
them: the first action of a task_id emits a `computer_session_start` event
and an idle task — no action for SESSION_IDLE_S, default 10 minutes — emits a
`computer_session_end` event on the next `note()`.

The sweep is lazy (no background task): endings are only noticed while the
daemon is doing something. A task that goes idle forever never emits `end` —
its start + actions remain complete facts for replay, so this is fine.

Config via env: MEIVA_COMPUTER_SESSION_IDLE_S (default 600).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

DEFAULT_IDLE_S = 600.0

SessionEvent = tuple[str, int, dict[str, Any]]


class _TaskSession:
    __slots__ = ("action_count", "agent_id", "first_action_at", "first_tool", "last_action_at")

    def __init__(self, agent_id: int, now: float, tool: str) -> None:
        self.agent_id = agent_id
        self.first_tool = tool
        self.first_action_at = now
        self.last_action_at = now
        self.action_count = 1


class TaskSessionTracker:
    """Lazy task-session envelope: start on first action, end after idle."""

    def __init__(self, idle_s: float = DEFAULT_IDLE_S) -> None:
        self._idle_s = idle_s
        self._sessions: dict[int, _TaskSession] = {}

    def note(self, task_id: int, agent_id: int, tool: str, *, now: float) -> list[SessionEvent]:
        events = self._sweep(now)
        sess = self._sessions.get(task_id)
        if sess is None:
            sess = _TaskSession(agent_id, now, tool)
            self._sessions[task_id] = sess
            events.append(
                (
                    "computer_session_start",
                    agent_id,
                    {
                        "task_id": task_id,
                        "first_tool": tool,
                        "first_action_at": _iso(now),
                    },
                )
            )
            return events
        sess.last_action_at = now
        sess.action_count += 1
        return events

    def _sweep(self, now: float) -> list[SessionEvent]:
        expired = [
            task_id
            for task_id, sess in self._sessions.items()
            if now - sess.last_action_at > self._idle_s
        ]
        events: list[SessionEvent] = []
        for task_id in expired:
            sess = self._sessions.pop(task_id)
            events.append(
                (
                    "computer_session_end",
                    sess.agent_id,
                    {
                        "task_id": task_id,
                        "action_count": sess.action_count,
                        "first_action_at": _iso(sess.first_action_at),
                        "last_action_at": _iso(sess.last_action_at),
                        "outcome": "idle_timeout",
                    },
                )
            )
        return events


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()
