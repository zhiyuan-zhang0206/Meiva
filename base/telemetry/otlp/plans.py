# meiva:note
# Pure OTLP export plans, attribute filters, and deferral config parsers.

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass

CHILD_DEFER_MAX_AGE_DEFAULT_S = 60.0
_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_SPELLINGS = frozenset({"0", "false", "no", "off"})
_NO_METRIC_ATTRS = frozenset({"body"})
_MAX_METRIC_ATTR_CHARS = 64

BASE_METRIC_DISPOSITION: dict[tuple[str, str], str | None] = {
    ("event_log_drop", "last_dropped_at"): "gauge",
    ("llm_usage", "price_miss"): None,
    ("llm_usage", "price_hit"): None,
    ("llm_usage", "price_out"): None,
    ("llm_usage", "cost_usd"): "counter",
    ("compaction_completed", "history_chars"): "histogram",
    ("compaction_completed", "summary_chars"): "histogram",
    ("resolution_status", "unresolved_warnings"): "gauge",
    ("resolution_status", "unresolved_errors"): "gauge",
    ("resolution_status", "dismissed_warnings"): "gauge",
    ("resolution_status", "dismissed_errors"): "gauge",
    ("pr_flow_daily", "merged_count"): "gauge",
    ("pr_flow_daily", "ready_to_merge_median_seconds"): "gauge",
    ("pr_flow_daily", "ready_to_merge_p90_seconds"): "gauge",
    ("pr_flow_daily", "flake_new_quarantines"): "gauge",
    ("pr_flow_run", "queue_depth"): "gauge",
    ("agent_registry", "max_id"): "gauge",
    ("memory_search_stats", "rows"): "gauge",
    ("memory_search_stats", "last_save_seconds"): "gauge",
    ("root_health_tick", "last_tick_timestamp_seconds"): "gauge",
    ("root_health_expected", "expected_since_timestamp_seconds"): "gauge",
    ("checkpoint_table_sizes", "blobs_bytes"): "gauge",
    ("checkpoint_table_sizes", "checkpoints_bytes"): "gauge",
    ("checkpoint_table_sizes", "writes_bytes"): "gauge",
    ("checkpoint_table_sizes", "blobs_live"): "gauge",
    ("checkpoint_table_sizes", "checkpoints_live"): "gauge",
    ("checkpoint_table_sizes", "writes_live"): "gauge",
    ("sse", "active_connections"): "gauge",
    ("gateway_process", "cpu_percent"): "gauge",
    ("gateway_process", "rss_bytes"): "gauge",
    ("gateway_process", "fd_count"): "gauge",
}


# meiva:note
# Parse a tristate child-defer boolean string; unrecognized values return ``default``.
def parse_defer_flag(raw: str | None, *, default: bool = True) -> bool:
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_SPELLINGS:
        return True
    if normalized in _FALSE_SPELLINGS:
        return False
    return default


# meiva:note
# Parse a positive finite max-age duration in seconds; invalid inputs return ``default``.
def parse_defer_seconds(raw: str | None, default: float = CHILD_DEFER_MAX_AGE_DEFAULT_S) -> float:
    if raw is None:
        return default
    normalized = raw.strip()
    if not normalized:
        return default
    try:
        value = float(normalized)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


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
    http_timeout_s: float = 2.0,
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


# meiva:note
# Select bounded datapoint attributes from an event's dimensions and declared payload.
def select_metric_attributes(
    *,
    machine: str,
    process: str,
    agent_id: int | None,
    attributes: Mapping[str, object],
    allowed_keys: Collection[str] | None,
) -> dict[str, str | int | float | bool]:
    attrs: dict[str, str | int | float | bool] = {"machine": machine, "process": process}
    if agent_id is not None:
        attrs["agent_id"] = agent_id
    if not allowed_keys:
        return attrs
    for key, value in attributes.items():
        if key in _NO_METRIC_ATTRS or key not in allowed_keys:
            continue
        if isinstance(value, bool) or (
            isinstance(value, str) and len(value) <= _MAX_METRIC_ATTR_CHARS
        ):
            attrs[key] = value
    return attrs
