# meiva:note
# The observability values every command in this package reads, taken once at the composition
# root.
#
# A command never reads `settings`: the entry point builds these frozen values from its
# configuration owner (`inputs.py`) and hands them down, so a test builds the values it wants.

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "AlertWebhookInputs",
    "DataPlaneInputs",
    "GrafanaCredentials",
    "ObservabilityConfig",
]


# meiva:note
# The telemetry endpoints, listen hosts, ports and retention of one unit.
@dataclass(frozen=True)
class ObservabilityConfig:
    observability_url: str
    observability_otlp_port: int
    telemetry_otlp_enabled: bool
    telemetry_otlp_port: int
    gateway_otlp_endpoint: str
    telemetry_tempo_endpoint: str
    telemetry_tempo_query_url: str
    telemetry_loki_url: str
    telemetry_prometheus_url: str
    telemetry_grafana_url: str
    lgtm_listen_host: str
    lgtm_grafana_listen_host: str
    lgtm_loki_port: int
    lgtm_loki_grpc_port: int
    lgtm_grafana_port: int
    trace_retention_days: int
    otel_collector_metrics_port: int
    lgtm_storage_dir: str = ""


# meiva:note
# Where Grafana's alert webhook can reach this cluster's gateway.
@dataclass(frozen=True)
class AlertWebhookInputs:
    gateway_url: str
    gateway_port: int


# meiva:note
# The credentials the rendered Grafana tree carries: the admin password when one is set and the
# Telegram bot that gets the direct contact point (an empty token or owner 0 means no bot). The
# password and the token never reach a repr, so a log line or a traceback that formats the value
# never prints them.
@dataclass(frozen=True)
class GrafanaCredentials:
    admin_password: str | None = field(repr=False)
    telegram_bot_token: str = field(repr=False)
    telegram_owner_id: int


# meiva:note
# The cluster data plane as the collector config reads it: the connection URLs, whether the
# plane lives off-box and the cluster secret the telemetry bearer derives from. The URLs carry the
# login and the two passwords are secrets: none of them reaches a repr.
@dataclass(frozen=True)
class DataPlaneInputs:
    db_url: str = field(repr=False)
    redis_url: str = field(repr=False)
    redis_admin_password: str = field(repr=False)
    is_remote: bool
    cluster_secret: str = field(repr=False)
