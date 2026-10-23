from pathlib import Path

from base.packages.plugins.tests.plugin_boundary import run_boundary


def test_load_failure_report_carries_the_traceback(tmp_path: Path) -> None:
    run_boundary("failed-report-writer", home=tmp_path, env={}, args=[])
