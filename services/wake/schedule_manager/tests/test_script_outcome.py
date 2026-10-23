from __future__ import annotations

import pytest

from services.wake.schedule_manager.script_outcome import (
    command_failure_message,
    command_timeout_message,
    crash_note,
    exit_code_of,
    exit_detail,
)


@pytest.mark.parametrize(
    ("code", "expected"),
    [(None, 0), (0, 0), (3, 3), (True, 1), (False, 0), ("done early", 1), (3.7, 1)],
)
def test_a_system_exit_argument_reads_like_the_interpreters(code: object, expected: int) -> None:
    assert exit_code_of(code) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [(None, ""), (3, ""), (True, ""), ("done early", ": done early"), (3.7, ": 3.7")],
)
def test_only_a_message_exit_argument_rides_the_note(code: object, expected: str) -> None:
    assert exit_detail(code) == expected


def test_a_short_traceback_is_kept_whole() -> None:
    assert (
        crash_note("RuntimeError", "Traceback\n  boom\n")
        == "crashed: RuntimeError\nTraceback\n  boom"
    )


def test_a_long_traceback_keeps_its_end() -> None:
    note = crash_note("RuntimeError", "x" * 5000 + "the-end")

    first, tail = note.split("\n", 1)
    assert first == "crashed: RuntimeError"
    assert tail.startswith("[...truncated]\n")
    assert tail.endswith("the-end")
    assert len(note) <= 3100


def test_command_failures_quote_the_command() -> None:
    assert command_failure_message(3, "bash run.sh") == "command exited 3: 'bash run.sh'"
    assert command_timeout_message(1200.0, "bash run.sh") == (
        "command did not finish within 1200s (stall timeout): 'bash run.sh'"
    )
