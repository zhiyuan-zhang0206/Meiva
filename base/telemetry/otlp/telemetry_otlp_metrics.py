# meiva:note
# Metric-side naming machinery and cadence constants for OTLP telemetry export.

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

from base.telemetry.otlp.plans import (
    ProviderBuildPlan as ProviderBuildPlan,
)
from base.telemetry.otlp.plans import (
    provider_build_plan as provider_build_plan,
)
from base.telemetry.otlp.plans import (
    strip_unit_suffix as strip_unit_suffix,
)
from base.telemetry.otlp.plans import (
    unit_for as unit_for,
)

OTLP_HTTP_TIMEOUT_S = 2.0

# Histogram bucket boundaries for LLM-scale latencies (ms). The OTel defaults
# top out at 10000 — every call slower than 10s fell into +Inf and clipped
# p95/p50 at exactly 10s on the ops panels.
LLM_LATENCY_BUCKETS_MS = (250, 500, 1000, 2000, 4000, 8000, 15000, 30000, 60000, 120000, 300000)

# A recovered event loop reports the whole stall on its next tick. Preserve
# minute-scale freezes instead of folding every delay above 10s into +Inf.
EVENT_LOOP_LAG_BUCKETS_MS = (
    1,
    5,
    10,
    25,
    50,
    100,
    250,
    500,
    1000,
    5000,
    10000,
    30000,
    60000,
    120000,
    300000,
)


# meiva:note
# The int and float fields of an event payload. The payload is typed ``dict[str, Any]`` by the
# event contract, so this reads it as ``object`` values and narrows each one here.
def numeric_fields(payload: Mapping[str, object]) -> Iterator[tuple[str, int | float]]:
    for key, value in payload.items():
        # meiva:note
        # bool must be checked before int — `isinstance(True, int)` is True.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        yield key, value


# meiva:note
# The attributes of the metrics-side OTel Resource: `service.name=meiva-<process>` (the bounded
# process dimension) plus a per-process-instance `service.instance.id`. They live here, with no SDK
# import, and `telemetry_otlp_sdk.metrics_resource` builds the Resource from them.
def metrics_resource_attributes(*, home: Path) -> dict[str, str]:
    import uuid
    from importlib.metadata import version

    from base.telemetry.delivery.state import process_name
    from base.telemetry.observability import cluster_label

    if not home.is_dir():
        raise RuntimeError(f"metrics resource home is not a directory: {home}")

    return {
        "service.namespace": "meiva",
        "service.name": f"meiva-{process_name()}",
        "service.instance.id": str(uuid.uuid4()),
        "service.version": version("meiva"),
        "cluster": cluster_label(home),
    }
