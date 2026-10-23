from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from base import telemetry
from base.packages.plugins.load_report import report_plugin_load_failure
from base.packages.plugins.tests.plugin_boundary import run_boundary


def test_load_failure_report_carries_the_traceback(
    loguru_records: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(telemetry, "emit", _broken)
    report_plugin_load_failure("demo", RuntimeError("boom"))

    record = next(r for r in loguru_records if "failed to load" in r["message"])
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError
    fallback = next(r for r in loguru_records if "was not emitted" in r["message"])
    assert fallback["extra"]["_no_emitter"] is True
    assert fallback["exception"].type is RuntimeError
    assert str(fallback["exception"].value) == "telemetry down"


def test_load_failure_report_reaches_a_failing_writer(tmp_path: Path) -> None:
    run_boundary("failed-report-writer", home=tmp_path, env={}, args=[])
