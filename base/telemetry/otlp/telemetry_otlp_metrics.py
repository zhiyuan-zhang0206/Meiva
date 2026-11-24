# meiva:note
# Metric-side naming machinery and cadence constants for OTLP telemetry export.

from __future__ import annotations

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

# Histogram bucket boundaries for LLM-scale latencies (ms). The OTel defaults
# top out at 10000 — every call slower than 10s fell into +Inf and clipped
# p95/p50 at exactly 10s on the ops panels.
LLM_LATENCY_BUCKETS_MS = (250, 500, 1000, 2000, 4000, 8000, 15000, 30000, 60000, 120000, 300000)


# meiva:note
# The attributes of the metrics-side OTel Resource: `service.name=meiva-<process>` (the bounded
# process dimension) plus `instance_id`, the `service.instance.id` the process root minted once.
# It arrives as data: this module reads no entropy, so a second provider built in the same process
# cannot acquire a second identity. They live here, with no SDK import, and
# `telemetry_otlp_sdk.metrics_resource` builds the Resource from them.
def metrics_resource_attributes(*, home: Path, instance_id: str) -> dict[str, str]:
    from importlib.metadata import version

    from base.telemetry.delivery.state import process_name
    from base.telemetry.observability import cluster_identity_attributes, cluster_label

    if not home.is_dir():
        raise RuntimeError(f"metrics resource home is not a directory: {home}")

    return {
        "service.namespace": "meiva",
        "service.name": f"meiva-{process_name()}",
        "service.instance.id": instance_id,
        "service.version": version("meiva"),
        "cluster": cluster_label(home),
        **cluster_identity_attributes(home),
    }
