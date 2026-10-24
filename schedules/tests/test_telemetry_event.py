# meiva:note
# A schedule's one telemetry event, emitted in a fresh interpreter because process logging is
# process-global state.

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from base.host.proc import run_bounded

_CHILD = Path(__file__).parent / "children" / "emit_child.py"


def test_the_event_is_emitted_once_process_logging_is_open(tmp_path: Path) -> None:
    child = run_bounded(
        [sys.executable, "-I", str(_CHILD), "schedule-probe"],
        env={**os.environ, "MEIVA_HOME": str(tmp_path), "MEIVA_CONFIG_FETCH": "skip"},
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert result["files"] == ["schedule-probe.log"]
    names = [event["name"] for event in result["events"]]
    assert names.count("service_started") == 1
    assert names.count("ci_usage_daily") == 2
    [started] = [event for event in result["events"] if event["name"] == "service_started"]
    assert started["attributes"]["name"] == "schedule-probe"
    assert started["attributes"]["host"] == "test-host"
    emitted = [event for event in result["events"] if event["name"] == "ci_usage_daily"]
    assert emitted[0]["attributes"] == {"runs": 3, "appended_runs": 1}
