from __future__ import annotations

from pydantic import Field

from base.config.base import EnvSettings


class TempoSettings(EnvSettings):
    telemetry_tempo_endpoint: str = Field(
        title="Tempo trace ingestion endpoint",
        default="http://127.0.0.1:14318",
        alias="MEIVA_TELEMETRY_TEMPO_ENDPOINT",
        description=(
            "Tempo OTLP/HTTP base URL. The single-box default is the host "
            "loopback. A cluster's Tempo may be remote through a host-scope "
            "MEIVA_TELEMETRY_TEMPO_ENDPOINT override, as configured on the "
            "production cluster. On the native station, the URL port is also "
            "the local Tempo OTLP/HTTP port. A pure runner ignores this URL and relays "
            "through the gateway."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    telemetry_tempo_query_url: str = Field(
        title="Tempo query URL",
        default="http://127.0.0.1:3200",
        alias="MEIVA_TELEMETRY_TEMPO_QUERY_URL",
        description=(
            "Tempo query/metrics base URL for the Grafana Tempo datasource and "
            "Prometheus scrape of the trace backend. The OTLP intake endpoint is "
            "MEIVA_TELEMETRY_TEMPO_ENDPOINT on a different port. On the native "
            "station, the query URL port is also Tempo's local HTTP port."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_tempo_grpc_port: int = Field(
        title="Tempo internal gRPC port",
        default=9096,
        ge=1,
        le=65535,
        alias="MEIVA_LGTM_TEMPO_GRPC_PORT",
        description="Internal gRPC port for native Tempo. The default keeps it distinct from Loki's 9095.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_tempo_retention_hours: int = Field(
        title="Tempo block retention (hours)",
        default=168,
        ge=1,
        alias="MEIVA_LGTM_TEMPO_RETENTION_HOURS",
        description="Retain one week of traces by default, preserving the station's existing incident-investigation window and disk budget.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
