# meiva:link base/deploy/docs/start-trace.meiva.okf.md

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from base.deploy import start_trace


def test_phases_are_recorded_in_start_order() -> None:
    trace = start_trace.StartTrace()
    trace.begin()
    with trace.phase("first"):
        time.sleep(0.001)
    with trace.phase("second"):
        pass

    rows = trace.rows()

    assert [row[0] for row in rows] == ["first", "second"]
    assert all(took >= 0.0 for _, _, took in rows)


def test_a_raising_phase_still_closes_its_span() -> None:
    trace = start_trace.StartTrace()
    trace.begin()
    with pytest.raises(RuntimeError), trace.phase("explodes"):
        raise RuntimeError("boom")

    assert [row[0] for row in trace.rows()] == ["explodes"]


def test_report_names_phases_services_and_total() -> None:
    trace = start_trace.StartTrace()
    trace.begin()
    with trace.phase("readiness wait"):
        pass
    trace.service_ready("frontend", 12.5)

    report = trace.render()

    assert "readiness wait" in report
    assert "ready: frontend" in report
    assert "total" in report


def test_report_is_none_unless_asked() -> None:
    assert start_trace.report(None, requested=False) is None


def test_report_renders_when_asked() -> None:
    report = start_trace.report(None, requested=True)

    assert report is not None and "start timing" in report


def test_write_keeps_one_json_record(tmp_path: Path) -> None:
    trace = start_trace.StartTrace()
    trace.begin()
    with trace.phase("phase"):
        pass

    path = trace.write(tmp_path)

    assert path is not None and path.parent == tmp_path / "logs" / "start-timing"
    payload = json.loads(path.read_text())
    assert payload["phases"][0]["name"] == "phase"
