from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded

_CHILD = Path(__file__).parent / "children" / "entry_process_child.py"
_SCHEDULES = Path(__file__).resolve().parents[1]


def _run(tmp_path: Path, *process: str) -> dict[str, object]:
    home = tmp_path / "home"
    home.mkdir()
    child = run_bounded(
        [sys.executable, str(_CHILD), str(home), *process],
        env={
            **os.environ,
            "PYTHONPATH": str(_SCHEDULES.parent),
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
        },
        cwd=_SCHEDULES.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr
    result: dict[str, object] = json.loads(child.stdout)
    return result


def test_an_unnamed_schedule_is_labelled_sdk_and_drops_its_metrics(tmp_path: Path) -> None:
    assert _run(tmp_path) == {"before_sdk": "unknown", "after_sdk": "sdk", "exported": False}


def test_a_named_schedule_keeps_its_process_across_the_first_sdk_call(tmp_path: Path) -> None:
    assert _run(tmp_path, "main-red-watch") == {
        "before_sdk": "main-red-watch",
        "after_sdk": "main-red-watch",
        "exported": True,
    }


@pytest.mark.parametrize(
    "script",
    [
        "jobs/watch/main-red-watch-schedule.py",
        "agent-watch-schedule.py",
        "c9-daily-report-schedule.py",
        "debt-sweep-daily-schedule.py",
        "jobs/harness/harness-cli-update-schedule.py",
        "jobs/harness/claude-auth-keepalive-schedule.py",
    ],
)
def test_every_schedule_that_emits_a_system_event_names_its_process_at_entry(script: str) -> None:
    tree = ast.parse((_SCHEDULES / script).read_text())
    [entry] = [
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "open_schedule_entry"
    ]
    assert "process" in {keyword.arg for keyword in entry.keywords}
