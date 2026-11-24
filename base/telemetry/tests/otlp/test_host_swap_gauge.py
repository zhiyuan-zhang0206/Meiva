from __future__ import annotations

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Gauge, InMemoryMetricReader

from base.telemetry.otlp.telemetry_otlp_gauges import (
    SWAP_USAGE_METRIC,
    read_swap_bytes,
    swap_usage_callback,
)

GIB = 1024**3


def _points(reader: InMemoryMetricReader) -> dict[str, float]:
    data = reader.get_metrics_data()
    assert data is not None
    (metric,) = (m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics)
    assert metric.name == "system.paging.usage"
    assert metric.unit == "By"
    assert isinstance(metric.data, Gauge)
    return {str((p.attributes or {})["state"]): p.value for p in metric.data.data_points}


def test_the_gauge_reports_the_pool_by_state_in_bytes() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")

    _ = meter.create_observable_gauge(
        SWAP_USAGE_METRIC, callbacks=[swap_usage_callback(lambda: (3 * GIB, 1 * GIB))], unit="By"
    )

    assert _points(reader) == {"used": 3 * GIB, "free": 1 * GIB}


def test_each_collection_reads_the_pool_again() -> None:
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader]).get_meter("test")
    pools = iter([(1 * GIB, 3 * GIB), (2 * GIB, 2 * GIB)])

    _ = meter.create_observable_gauge(
        SWAP_USAGE_METRIC, callbacks=[swap_usage_callback(lambda: next(pools))], unit="By"
    )

    assert _points(reader)["used"] == 1 * GIB
    assert _points(reader)["used"] == 2 * GIB


def test_the_host_read_returns_a_consistent_nonnegative_pool() -> None:
    used, free = read_swap_bytes()

    assert used >= 0
    assert free >= 0
