from __future__ import annotations

from pathlib import Path
from typing import Any

from base.packages.plugins.load_report import report_plugin_load_failure
from base.packages.plugins.tests.plugin_boundary import run_boundary


class _UnprintableError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("telemetry down")


def test_load_failure_report_carries_the_traceback(
    loguru_records: list[dict[str, Any]],
) -> None:
    report_plugin_load_failure("demo", _UnprintableError("boom"))

    record = next(r for r in loguru_records if "failed to load" in r["message"])
    assert record["exception"] is not None
    assert issubclass(record["exception"].type, RuntimeError)
    fallback = next(r for r in loguru_records if "was not emitted" in r["message"])
    assert fallback["extra"]["_no_emitter"] is True
    assert fallback["exception"].type is RuntimeError
    assert str(fallback["exception"].value) == "telemetry down"


def test_load_failure_report_reaches_a_failing_writer(tmp_path: Path) -> None:
    run_boundary("failed-report-writer", home=tmp_path, env={}, args=[])
