# meiva:note
# Fresh-interpreter proof that importing the CI metrics schedule job does not load the exporter.

from __future__ import annotations

import sys


def main() -> None:
    from schedules.jobs import ci_metrics

    assert callable(ci_metrics.collect_ci_metrics)
    assert "scripts.ci.pull_requests.runs_api" not in sys.modules
    assert "scripts.ci.pull_requests.runs_export" not in sys.modules
    assert "scripts.ci.pull_requests.export_process" not in sys.modules


if __name__ == "__main__":
    main()
