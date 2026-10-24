# meiva:note
# Shell transcripts and service output are tailed by two receivers over disjoint file sets, each
# bounded in discovery. Agent main logs begin with identical telemetry banners, so admitting them
# to either receiver would restore the fingerprint-collision re-watch storm.

from __future__ import annotations

from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    HYBRID,
    CollectorUnit,
)
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    collector_unit as collector_unit,
)
from cli.database.tests.command_inputs import command_runtime as command_runtime


def test_session_filelog_receivers_are_disjoint_and_bound_discovery(
    collector_unit: CollectorUnit,
) -> None:
    config = collector_unit.render(HYBRID)
    logs = f"{collector_unit.home}/logs"

    assert config["receivers"]["filelog/sessions"] == {
        "include": [f"{logs}/meiva-agent-*-shell-*.out.log"],
        "start_at": "end",
        "include_file_name": True,
        "include_file_path": False,
        "storage": "file_storage/logoffsets",
        "poll_interval": "30s",
        "polls_to_archive": 50,
        "max_concurrent_files": 200,
    }
    assert config["receivers"]["filelog/services"] == {
        "include": [f"{logs}/*.out.log"],
        "exclude": [f"{logs}/meiva-agent-*.out.log", f"{logs}/meiva-otel-collector.out.log"],
        "start_at": "end",
        "include_file_name": True,
        "include_file_path": False,
        "storage": "file_storage/logoffsets",
        "poll_interval": "30s",
        "polls_to_archive": 50,
        "max_concurrent_files": 200,
    }
