from __future__ import annotations

from services.supervision.healthchecks.plans.collector_metrics import (
    CollectorPressure,
    parse_pressure,
)


def test_an_empty_scrape_has_no_pressure() -> None:
    assert parse_pressure("") == CollectorPressure(saturated=(), enqueue_failures=())


def test_only_a_full_queue_with_capacity_is_saturated() -> None:
    text = "\n".join(
        [
            'otelcol_exporter_queue_capacity{exporter="a"} 10',
            'otelcol_exporter_queue_size{exporter="a"} 10',
            'otelcol_exporter_queue_capacity{exporter="b"} 10',
            'otelcol_exporter_queue_size{exporter="b"} 9',
            'otelcol_exporter_queue_capacity{exporter="c"} 0',
            'otelcol_exporter_queue_size{exporter="c"} 0',
        ]
    )

    assert parse_pressure(text).saturated == ("a",)


def test_enqueue_failures_add_up_per_exporter() -> None:
    text = "\n".join(
        [
            'otelcol_exporter_enqueue_failed_metric_points{exporter="a"} 3',
            'otelcol_exporter_enqueue_failed_log_records{exporter="a"} 4',
            'otelcol_exporter_enqueue_failed_spans{exporter="b"} 1.0',
            'otelcol_exporter_enqueue_failed_spans{other="x"} 9',
        ]
    )

    assert parse_pressure(text).enqueue_failures == (("a", 7), ("b", 1))
