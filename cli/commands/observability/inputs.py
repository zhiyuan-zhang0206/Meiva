# meiva:note
# Reads the observability values a command needs from its configuration owner.
#
# A command's composition point calls one of these once with its `ConfigBoot`; everything below
# receives the frozen values. Importing `ConfigBoot` loads only the boot-lite facade, never the
# eager configuration chain, so a settings-free command that imports this module stays settings
# free until it reads a field outside the boot index.

from __future__ import annotations

from dataclasses import dataclass

from base.config import ConfigBoot
from base.deploy.release.collector import plan as collector_plan
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
    "collector_settings_from_boot",
    "lgtm_settings_from_boot",
    "observability_config_from_boot",
]


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
    download: collector_plan.DownloadLimits


# meiva:note
# The telemetry endpoints, listen hosts, ports and retention of this unit.
def observability_config_from_boot(boot: ConfigBoot) -> ObservabilityConfig:
    domain = boot.view.observability
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
        lgtm_storage_dir=domain.lgtm_storage_dir,
    )


# meiva:note
# The bounds of one native LGTM archive download.
def _lgtm_download_limits(boot: ConfigBoot) -> DownloadLimits:
    domain = boot.view.observability
    return DownloadLimits(
        total_budget_s=domain.lgtm_native_download_budget_s,
        socket_timeout_s=domain.lgtm_native_download_socket_timeout_s,
        attempts=domain.lgtm_native_download_attempts,
        retry_backoff_s=domain.lgtm_native_download_retry_backoff_s,
        progress_interval_s=domain.lgtm_native_download_progress_interval_s,
    )


# meiva:note
# The bounds of the pinned collector download, in the shape the release owner takes.
def _collector_download_limits(boot: ConfigBoot) -> collector_plan.DownloadLimits:
    domain = boot.view.observability
    return collector_plan.DownloadLimits(
        total_budget_s=domain.otel_collector_download_budget_s,
        socket_timeout_s=domain.otel_collector_download_socket_timeout_s,
        attempts=domain.otel_collector_download_attempts,
        retry_backoff_s=domain.otel_collector_download_retry_backoff_s,
        progress_interval_s=domain.otel_collector_download_progress_interval_s,
    )


# meiva:note
# The cluster's data-plane URLs, admin password, locality and secret.
def _data_plane_inputs(boot: ConfigBoot) -> DataPlaneInputs:
    domain = boot.view.data_plane
    return DataPlaneInputs(
        db_url=domain.db_url,
        redis_url=domain.redis_url,
        redis_admin_password=domain.redis_admin_password,
        is_remote=domain.is_remote,
        cluster_secret=domain.cluster_secret,
    )


# meiva:note
# The gateway base URL and bind port the Grafana alert webhook dials.
def _alert_webhook_inputs(boot: ConfigBoot) -> AlertWebhookInputs:
    domain = boot.view.gateway
    return AlertWebhookInputs(gateway_url=domain.gateway_url, gateway_port=domain.gateway_port)


# meiva:note
# The Grafana admin password (when configured, read out of its secret wrapper here and nowhere
# else) and the Telegram bot of this home.
def _grafana_credentials(boot: ConfigBoot) -> GrafanaCredentials:
    view = boot.view
    password = view.alerts.grafana_admin_password
    return GrafanaCredentials(
        admin_password=None if password is None else password.get_secret_value(),
        telegram_bot_token=view.telegram.telegram_bot_token,
        telegram_owner_id=view.telegram.telegram_owner_id,
    )


# meiva:note
# The configuration the native LGTM converge reads.
def lgtm_settings_from_boot(boot: ConfigBoot) -> LgtmSettings:
    return LgtmSettings(
        observability=observability_config_from_boot(boot),
        webhook=_alert_webhook_inputs(boot),
        credentials=_grafana_credentials(boot),
        download=_lgtm_download_limits(boot),
    )


# meiva:note
# The configuration the collector sidecar converge reads.
def collector_settings_from_boot(boot: ConfigBoot) -> CollectorSettings:
    return CollectorSettings(
        observability=observability_config_from_boot(boot),
        data_plane=_data_plane_inputs(boot),
        download=_collector_download_limits(boot),
    )
