# meiva:note
# What the collector's own metrics say about its exporter queues, as a function of the scraped
# text: which exporters have a full queue and how many items each has failed to enqueue. The
# scrape itself belongs to `otel_collector.py`.

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["CollectorPressure", "parse_pressure"]

_QUEUE_SAMPLE = re.compile(
    r"^otelcol_exporter_queue_(?P<kind>capacity|size)\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.eE+-]+)$"
)
_ENQUEUE_FAILURE_SAMPLE = re.compile(
    r"^otelcol_exporter_enqueue_failed_[^{]+\{(?P<labels>[^}]*)\}\s+(?P<value>[0-9.eE+-]+)$"
)
_LABEL = re.compile(r'(?:^|,)\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)="(?P<value>[^"]*)"')


# meiva:note
# The exporters whose queue is full right now, and the lifetime enqueue-failure counters.
@dataclass(frozen=True)
class CollectorPressure:
    saturated: tuple[str, ...]
    enqueue_failures: tuple[tuple[str, int], ...]


def _labels(text: str) -> dict[str, str]:
    return {match.group("key"): match.group("value") for match in _LABEL.finditer(text)}


# meiva:note
# Current saturation plus lifetime enqueue-failure counters. The counter is reported only as
# context while a queue is currently full; it is monotone for the collector process and
# therefore must never, by itself, create a permanent warning after recovery. Central alerting
# uses `increase(...[5m])` on the self-scraped series instead.
def parse_pressure(payload: str) -> CollectorPressure:
    capacities: dict[str, float] = {}
    sizes: dict[str, float] = {}
    failures: dict[str, int] = {}
    for line in payload.splitlines():
        queue_match = _QUEUE_SAMPLE.match(line)
        if queue_match:
            exporter = _labels(queue_match.group("labels")).get("exporter")
            if exporter:
                target = capacities if queue_match.group("kind") == "capacity" else sizes
                target[exporter] = float(queue_match.group("value"))
            continue
        failure_match = _ENQUEUE_FAILURE_SAMPLE.match(line)
        if failure_match:
            exporter = _labels(failure_match.group("labels")).get("exporter")
            if exporter:
                failures[exporter] = failures.get(exporter, 0) + int(
                    float(failure_match.group("value"))
                )
    saturated = tuple(
        sorted(
            exporter
            for exporter, capacity in capacities.items()
            if capacity > 0 and sizes.get(exporter, 0) >= capacity
        )
    )
    return CollectorPressure(saturated=saturated, enqueue_failures=tuple(sorted(failures.items())))
