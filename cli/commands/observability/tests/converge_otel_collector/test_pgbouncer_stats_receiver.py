from __future__ import annotations

import socket

from base.paths import otel_collector_active_config
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    HYBRID,
    CollectorUnit,
    config_of,
    mapping_at,
    sequence_at,
)
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    collector_unit as collector_unit,
)
from cli.commands.observability.tests.observability_inputs import observability


def _infra_receivers(config: object) -> list[object]:
    return sequence_at(config, "service", "pipelines", "metrics/infra", "receivers")


def test_a_gateway_reads_pgbouncers_stats_log_into_two_gauges_off_the_logs_pipeline(
    collector_unit: CollectorUnit,
) -> None:
    config = collector_unit.render(HYBRID)

    receiver = mapping_at(config, "receivers", "filelog/pgbouncer")
    assert receiver["include"] == [f"{collector_unit.home}/pgbouncer/pgbouncer.log"]
    assert receiver["start_at"] == "end"
    assert receiver["storage"] == "file_storage/logoffsets"
    [parser] = sequence_at(receiver, "operators")
    assert isinstance(parser, dict)
    assert parser["on_error"] == "drop_quiet"
    gauges = sequence_at(config, "connectors", "signal_to_metrics/pgbouncer", "logs")
    assert [(entry["name"], entry["unit"]) for entry in gauges if isinstance(entry, dict)] == [
        ("pgbouncer.transactions", "{transaction}/s"),
        ("pgbouncer.query_time", "us"),
    ]
    pipelines = mapping_at(config, "service", "pipelines")
    assert sequence_at(pipelines, "logs/pgbouncer", "receivers") == ["filelog/pgbouncer"]
    assert sequence_at(pipelines, "logs/pgbouncer", "exporters") == ["signal_to_metrics/pgbouncer"]
    assert "filelog/pgbouncer" not in sequence_at(pipelines, "logs", "receivers")
    assert "forward/harness" in mapping_at(config, "connectors")


def test_the_connectors_section_is_rendered_once_and_survives_the_harness_fallback(
    collector_unit: CollectorUnit,
) -> None:
    assert collector_unit.text(HYBRID).count("\nconnectors:") == 1
    _ = collector_unit.install_pinned_binary()
    path = otel_collector_active_config(collector_unit.home)
    with socket.socket() as occupant:
        occupant.bind(("127.0.0.1", 0))
        occupant.listen()
        settings = observability(harness_otlp_port=occupant.getsockname()[1])
        collector_unit.ensure(HYBRID, observability=settings)
        fallback = config_of(path.read_text())

    assert "forward/harness" not in mapping_at(fallback, "connectors")
    assert "otlp/harness" not in mapping_at(fallback, "receivers")
    assert set(mapping_at(fallback, "connectors")) == {"signal_to_metrics/pgbouncer"}
    pipelines = mapping_at(fallback, "service", "pipelines")
    assert sequence_at(pipelines, "logs/pgbouncer", "exporters") == ["signal_to_metrics/pgbouncer"]
    assert "signal_to_metrics/pgbouncer" in _infra_receivers(fallback)
