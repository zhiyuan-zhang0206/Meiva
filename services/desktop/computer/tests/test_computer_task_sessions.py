"""Unit tests for services/desktop/computer/task_sessions.py — task-session envelope.

Covers start-on-first-action, action counting, and the lazy idle sweep that
emits computer_session_end. The daemon integration (note() wired into
_call_tool after touch) is covered in test_computer_mcp_daemon.py.
"""

from __future__ import annotations

from services.desktop.computer.task_sessions import SessionEvent, TaskSessionTracker


def _recorder() -> tuple[list[SessionEvent], TaskSessionTracker]:
    return [], TaskSessionTracker(idle_s=60.0)


def test_first_action_emits_start_once() -> None:
    events, t = _recorder()
    events.extend(t.note(11, 7, "click", now=1000.0))
    events.extend(t.note(11, 7, "type_text", now=1001.0))
    starts = [e for e in events if e[0] == "computer_session_start"]
    assert len(starts) == 1
    assert starts[0][1] == 7
    assert starts[0][2]["task_id"] == 11
    assert starts[0][2]["first_tool"] == "click"


def test_actions_count_within_session() -> None:
    events, t = _recorder()
    events.extend(t.note(11, 7, "click", now=1000.0))
    events.extend(t.note(11, 7, "key", now=1001.0))
    events.extend(t.note(11, 7, "key", now=1002.0))
    # still live: three actions, no end event yet
    assert not [e for e in events if e[0] == "computer_session_end"]
    # the task goes idle past the threshold; the next note sweeps it
    events.extend(t.note(12, 8, "click", now=1063.0))
    ends = [e for e in events if e[0] == "computer_session_end"]
    assert len(ends) == 1
    assert ends[0][1] == 7
    assert ends[0][2]["task_id"] == 11
    assert ends[0][2]["action_count"] == 3
    assert ends[0][2]["outcome"] == "idle_timeout"


def test_idle_task_emits_end_on_next_note() -> None:
    events, t = _recorder()
    events.extend(t.note(11, 7, "click", now=1000.0))
    # task 11 goes idle (its last action is now > idle_s in the past)
    events.extend(t.note(12, 8, "click", now=1061.0))
    ends = [e for e in events if e[0] == "computer_session_end"]
    assert len(ends) == 1
    assert ends[0][2]["task_id"] == 11
    assert ends[0][2]["outcome"] == "idle_timeout"
    # task 11 is forgotten — a later action starts a fresh session
    events.extend(t.note(11, 7, "click", now=1062.0))
    starts = [e for e in events if e[0] == "computer_session_start"]
    assert len(starts) == 3


def test_recent_task_not_swept() -> None:
    events, t = _recorder()
    events.extend(t.note(11, 7, "click", now=1000.0))
    events.extend(t.note(12, 8, "click", now=1010.0))
    ends = [e for e in events if e[0] == "computer_session_end"]
    assert ends == []
