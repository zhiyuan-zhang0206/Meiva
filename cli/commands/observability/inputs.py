# meiva:note
# Reads the observability values a command needs from its configuration owner.
#
# A command's composition point calls these once with the domains of its `ConfigBoot` view (or
# the process-wide `settings`); everything below receives the frozen values. The domains are
# typed by the fields the readers take, not by the settings models: importing the models would
# build the eager configuration chain the boot-lite path defers.

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from base.deploy.release import collector_artifact
from cli.commands.observability.plans.lgtm.download_retry import DownloadLimits
from cli.commands.observability.plans.observability_config import (
    AlertWebhookInputs,
    DataPlaneInputs,
    GrafanaCredentials,
    ObservabilityConfig,
)

__all__ = [
    "CollectorSettings",
    "LgtmSettings",
    "alert_webhook_inputs",
    "collector_download_limits",
    "collector_settings",
    "data_plane_inputs",
    "grafana_credentials",
    "lgtm_download_limits",
    "lgtm_settings",
    "observability_config",
]


# meiva:note
# The observability fields the readers below take. A `ConfigBoot` view domain, the process-wide
# `settings.observability` and the settings model all satisfy it, so the readers import none of
# them: the eager configuration models stay unloaded on the boot-lite path.
class ObservabilityDomain(Protocol):
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
    lgtm_native_download_budget_s: float
    lgtm_native_download_socket_timeout_s: float
    lgtm_native_download_attempts: int
    lgtm_native_download_retry_backoff_s: float
    lgtm_native_download_progress_interval_s: float
    otel_collector_download_budget_s: float
    otel_collector_download_socket_timeout_s: float
    otel_collector_download_attempts: int
    otel_collector_download_retry_backoff_s: float
    otel_collector_download_progress_interval_s: float


# meiva:note
# The data-plane fields the collector reads.
class DataPlaneDomain(Protocol):
    db_url: str
    redis_url: str
    redis_admin_password: str
    cluster_secret: str

    @property
    def is_remote(self) -> bool: ...


# meiva:note
# The gateway fields the Grafana alert webhook reads.
class GatewayDomain(Protocol):
    gateway_url: str
    gateway_port: int


class SecretValue(Protocol):
    def get_secret_value(self) -> str: ...


# meiva:note
# The Grafana admin password setting: a secret, or None when none is configured.
class AlertsDomain(Protocol):
    @property
    def grafana_admin_password(self) -> SecretValue | None: ...


class TelegramDomain(Protocol):
    telegram_bot_token: str
    telegram_owner_id: int


# meiva:note
# Everything the native LGTM renderer reads from configuration: the telemetry values, where the
# alert webhook dials, the Grafana credentials and the archive download bounds.
@dataclass(frozen=True)
class LgtmSettings:
    observability: ObservabilityConfig
    webhook: AlertWebhookInputs
    credentials: GrafanaCredentials
    download: DownloadLimits


# meiva:note
# Everything the collector sidecar converge reads from configuration: the telemetry values, the
# cluster data plane and the pinned archive download bounds.
@dataclass(frozen=True)
class CollectorSettings:
    observability: ObservabilityConfig
    data_plane: DataPlaneInputs
    download: collector_artifact.DownloadLimits


# meiva:note
# The telemetry endpoints, listen hosts, ports and retention of this unit.
def observability_config(domain: ObservabilityDomain) -> ObservabilityConfig:
    return ObservabilityConfig(
        observability_url=domain.observability_url,
        observability_otlp_port=domain.observability_otlp_port,
        telemetry_otlp_enabled=domain.telemetry_otlp_enabled,
        telemetry_otlp_port=domain.telemetry_otlp_port,
        gateway_otlp_endpoint=domain.gateway_otlp_endpoint,
        telemetry_tempo_endpoint=domain.telemetry_tempo_endpoint,
        telemetry_tempo_query_url=domain.telemetry_tempo_query_url,
        telemetry_loki_url=domain.telemetry_loki_url,
        telemetry_prometheus_url=domain.telemetry_prometheus_url,
        telemetry_grafana_url=domain.telemetry_grafana_url,
        lgtm_listen_host=domain.lgtm_listen_host,
        lgtm_grafana_listen_host=domain.lgtm_grafana_listen_host,
        lgtm_loki_port=domain.lgtm_loki_port,
        lgtm_loki_grpc_port=domain.lgtm_loki_grpc_port,
        lgtm_grafana_port=domain.lgtm_grafana_port,
        trace_retention_days=domain.trace_retention_days,
        otel_collector_metrics_port=domain.otel_collector_metrics_port,
    )


# meiva:note
# The bounds of one native LGTM archive download.
def lgtm_download_limits(domain: ObservabilityDomain) -> DownloadLimits:
    return DownloadLimits(
        total_budget_s=domain.lgtm_native_download_budget_s,
        socket_timeout_s=domain.lgtm_native_download_socket_timeout_s,
        attempts=domain.lgtm_native_download_attempts,
        retry_backoff_s=domain.lgtm_native_download_retry_backoff_s,
        progress_interval_s=domain.lgtm_native_download_progress_interval_s,
    )


# meiva:note
# The bounds of the pinned collector download, in the shape the release owner takes.
def collector_download_limits(domain: ObservabilityDomain) -> collector_artifact.DownloadLimits:
    return collector_artifact.DownloadLimits(
        total_budget_s=domain.otel_collector_download_budget_s,
        socket_timeout_s=domain.otel_collector_download_socket_timeout_s,
        attempts=domain.otel_collector_download_attempts,
        retry_backoff_s=domain.otel_collector_download_retry_backoff_s,
        progress_interval_s=domain.otel_collector_download_progress_interval_s,
    )


# meiva:note
# The cluster's data-plane URLs, admin password, locality and secret.
def data_plane_inputs(domain: DataPlaneDomain) -> DataPlaneInputs:
    return DataPlaneInputs(
        db_url=domain.db_url,
        redis_url=domain.redis_url,
        redis_admin_password=domain.redis_admin_password,
        is_remote=domain.is_remote,
        cluster_secret=domain.cluster_secret,
    )


# meiva:note
# The gateway base URL and bind port the Grafana alert webhook dials.
def alert_webhook_inputs(domain: GatewayDomain) -> AlertWebhookInputs:
    return AlertWebhookInputs(gateway_url=domain.gateway_url, gateway_port=domain.gateway_port)


# meiva:note
# The Grafana admin password (when configured) and the Telegram bot of this home.
def grafana_credentials(alerts: AlertsDomain, telegram: TelegramDomain) -> GrafanaCredentials:
    password = alerts.grafana_admin_password
    return GrafanaCredentials(
        admin_password=None if password is None else password.get_secret_value(),
        telegram_bot_token=telegram.telegram_bot_token,
        telegram_owner_id=telegram.telegram_owner_id,
    )


# meiva:note
# The configuration the native LGTM converge reads, from the domains of one owner.
def lgtm_settings(
    *,
    observability: ObservabilityDomain,
    gateway: GatewayDomain,
    alerts: AlertsDomain,
    telegram: TelegramDomain,
) -> LgtmSettings:
    return LgtmSettings(
        observability=observability_config(observability),
        webhook=alert_webhook_inputs(gateway),
        credentials=grafana_credentials(alerts, telegram),
        download=lgtm_download_limits(observability),
    )


# meiva:note
# The configuration the collector sidecar converge reads, from the domains of one owner.
def collector_settings(
    *, observability: ObservabilityDomain, data_plane: DataPlaneDomain
) -> CollectorSettings:
    return CollectorSettings(
        observability=observability_config(observability),
        data_plane=data_plane_inputs(data_plane),
        download=collector_download_limits(observability),
    )
