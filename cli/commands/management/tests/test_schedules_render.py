from __future__ import annotations

import pytest

from cli.commands.management.schedules import ScheduleRow, print_schedule


def _row(**overrides: object) -> ScheduleRow:
    fields: dict[str, object] = {
        "id": 2,
        "name": "memory-arbiter",
        "command": "python schedule.py",
        "enabled": True,
        "status": "running",
        "machine": "ubuntu",
    }
    fields.update(overrides)
    return ScheduleRow.model_validate(fields)


def _status_line(out: str) -> int:
    lines = out.splitlines()
    return next(i for i, line in enumerate(lines) if line.strip().startswith("status:"))


def test_the_status_line_says_it_is_not_a_liveness_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_schedule(_row(), with_script=False)
    lines = capsys.readouterr().out.splitlines()
    assert "not a liveness check" in lines[_status_line("\n".join(lines))]


def test_a_recorded_failure_sits_directly_under_the_status_it_disagrees_with(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # meiva:note
    # The shape an operator met on 2026-11-04: the runner died inside a live session, so the
    # bookkeeping still said `running` while `last_error` held the traceback. Adjacency is the fix.
    print_schedule(
        _row(last_error="TypeError: run_loop() got an unexpected keyword argument 'home'"),
        with_script=False,
    )
    lines = capsys.readouterr().out.splitlines()
    after_status = lines[_status_line("\n".join(lines)) + 1]
    assert after_status.strip().startswith("last_error:")
    assert "TypeError" in after_status


def test_a_failed_row_and_a_healthy_row_cannot_be_confused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_schedule(_row(), with_script=False)
    healthy = capsys.readouterr().out
    print_schedule(_row(last_error="boom"), with_script=False)
    failed = capsys.readouterr().out
    assert "last_error" not in healthy
    assert "last_error" in failed
