"""Thread-safe absolute-state helpers for OTLP ObservableGauge callbacks."""

from __future__ import annotations

import threading
from collections.abc import Callable

from opentelemetry.metrics import Observation

# meiva:note
# The datapoint attributes of one metric series: the bounded dimensions `select_metric_attributes`
# lets through.
type MetricAttributes = dict[str, str | int | float | bool]

GaugeValues = dict[
    tuple[str, str],
    dict[tuple[tuple[str, str | int | float | bool], ...], tuple[float, MetricAttributes]],
]


def record_gauge(
    gauge_values: GaugeValues,
    gauge_lock: threading.Lock,
    key: tuple[str, str],
    value: int | float,
    attrs: MetricAttributes,
    *,
    max_only: bool = False,
) -> None:
    """Replace an absolute value, or retain the greatest observed timestamp."""

    series = tuple(sorted(attrs.items(), key=lambda item: item[0]))
    with gauge_lock:
        values = gauge_values.setdefault(key, {})
        if max_only and series in values:
            value = max(value, values[series][0])
        values[series] = (float(value), dict(attrs))


def observable_gauge_callback(
    gauge_values: GaugeValues, gauge_lock: threading.Lock, key: tuple[str, str]
) -> Callable[[object], list[Observation]]:
    """Build the OTel callback that snapshots one gauge's current series."""

    def observe(_options: object) -> list[Observation]:
        with gauge_lock:
            values = list(gauge_values.get(key, {}).values())
        return [Observation(value, attributes=attrs) for value, attrs in values]

    return observe
