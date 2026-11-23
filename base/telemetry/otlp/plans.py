# meiva:note
# Pure OTLP export plans and attribute filters.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

METRIC_DISPOSITION: dict[str, dict[str, Literal["counter", "histogram", "gauge"]]] = {
    "agent_registry": {"max_id": "gauge"},
    "alerts": {"last_ingest_timestamp_seconds": "gauge"},
    "backup_dump_status": {"last_run_ok": "gauge"},
    "checkpoint_table_sizes": {
        "blobs_bytes": "gauge",
        "checkpoints_bytes": "gauge",
        "writes_bytes": "gauge",
    },
    "event_log_drop": {"last_dropped_at": "gauge"},
    "gateway_latency": {"count": "counter"},
    "llm_usage": {"latency_ms": "histogram"},
    "memory_search_stats": {"last_save_seconds": "gauge", "rows": "gauge"},
    "recovery_drill_status": {"last_run_ok": "gauge"},
    "root_health_expected": {"expected_since_timestamp_seconds": "gauge"},
    "root_health_tick": {"last_tick_timestamp_seconds": "gauge"},
    "schedule_manager_state": {
        "convergence_abandoned": "gauge",
        "process_lost": "gauge",
        "stalled": "gauge",
    },
    "trace_recording": {"disabled": "gauge"},
    "turn_end": {"duration_seconds": "histogram"},
}

LOG_ONLY_PROCESSES = frozenset({"agent-exec", "sdk", "schedule-runner", "cli-health-probe"})


# meiva:note
# Pure configuration plan for constructing the OTLP LoggerProvider and MeterProvider.
@dataclass(frozen=True)
class ProviderBuildPlan:
    logs_endpoint: str
    metrics_endpoint: str
    http_timeout_s: float
    export_timeout_millis: float
    metrics_interval_millis: float
    shutdown_on_exit: bool = False


# meiva:note
# Derive the OTLP/HTTP endpoints and processor timeouts for ``endpoint``.
def provider_build_plan(
    endpoint: str,
    *,
    http_timeout_s: float,
    metrics_interval_s: float = 15.0,
) -> ProviderBuildPlan:
    base_url = endpoint.rstrip("/")
    export_timeout_millis = http_timeout_s * 1000.0
    return ProviderBuildPlan(
        logs_endpoint=f"{base_url}/v1/logs",
        metrics_endpoint=f"{base_url}/v1/metrics",
        http_timeout_s=http_timeout_s,
        export_timeout_millis=export_timeout_millis,
        metrics_interval_millis=metrics_interval_s * 1000.0,
        shutdown_on_exit=False,
    )


# meiva:note
# Return the OTel unit for a payload field name.
def unit_for(field: str) -> str:
    if field.endswith("_ms"):
        return "ms"
    if field.endswith("_seconds") or field in ("duration", "latency"):
        return "s"
    return "1"


# meiva:note
# Strip ``_ms`` or ``_seconds`` suffix from a payload field name for instrument naming.
def strip_unit_suffix(field: str) -> str:
    for suffix in ("_ms", "_seconds"):
        if field.endswith(suffix):
            return field[: -len(suffix)]
    return field


def select_metric_attributes(
    event_name: str, process: str, attributes: Mapping[str, object]
) -> dict[str, str | int | float | bool]:
    """Select the dimensions Grafana uses. The source collector stamps machine."""
    if event_name == "event_log_drop":
        queue = attributes.get("queue")
        attrs: dict[str, str | int | float | bool] = {"process": process}
        if isinstance(queue, str) and queue in {"emitter", "otlp", "otel-sdk"}:
            attrs["queue"] = queue
        return attrs
    if event_name == "recovery_drill_status" and attributes.get("drill") == "logical_dump":
        return {"drill": "logical_dump"}
    if event_name in {"root_health_expected", "root_health_tick"}:
        home_id = attributes.get("home_id")
        if (
            isinstance(home_id, str)
            and len(home_id) == 64
            and all(char in "0123456789abcdef" for char in home_id)
        ):
            return {"process": process, "home_id": home_id}
        return {"process": process}
    return {}
