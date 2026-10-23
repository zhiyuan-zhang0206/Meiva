from __future__ import annotations

from typing import Any

from base.packages.plugins.load_report import report_plugin_load_failure


def test_load_failure_report_carries_the_traceback(
    loguru_records: list[dict[str, Any]],
) -> None:
    def _broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("telemetry down")

    report_plugin_load_failure("demo", RuntimeError("boom"), emit_fn=_broken)

    record = next(r for r in loguru_records if "failed to load" in r["message"])
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError
