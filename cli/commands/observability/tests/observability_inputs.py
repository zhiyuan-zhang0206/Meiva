# meiva:note
# The observability values the tests of this package start from.
#
# `observability()` is a unit with the settings defaults: loopback read URLs, the standard OTLP
# port, the wildcard Grafana bind. A test changes what it cares about by keyword, so nothing here
# patches `settings` or an environment variable.

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from base.cluster import ClusterPorts

from cli.commands.observability.inputs import CollectorSettings, LgtmSettings
from cli.commands.observability.plans.lgtm.download_retry import DownloadLimits
from cli.commands.observability.plans.observability_config import (
    AlertWebhookInputs,
    DataPlaneInputs,
    GrafanaCredentials,
    ObservabilityConfig,
)

__all__ = [
    "FAST_DOWNLOADS",
    "REPO_ROOT",
    "collector_settings",
    "data_plane",
    "lgtm_settings",
    "observability",
    "write_cluster_record",
]

# meiva:note
# The checkout the shells render from: the shipped templates, pins and provisioning tree.
REPO_ROOT = Path(__file__).resolve().parents[4]

# meiva:note
# Three attempts with a 10 ms backoff and a generous budget: a retrying download finishes in
# milliseconds.
FAST_DOWNLOADS = DownloadLimits(
    total_budget_s=30.0,
    socket_timeout_s=5.0,
    attempts=3,
    retry_backoff_s=0.01,
    progress_interval_s=15.0,
)

# meiva:note
# The settings defaults. The Grafana wildcard bind is the shipped default value, not a bind.
_DEFAULTS = ObservabilityConfig(
    observability_url="",
    observability_otlp_port=4318,
    telemetry_otlp_enabled=True,
    telemetry_otlp_port=4318,
    gateway_otlp_endpoint="",
    telemetry_tempo_endpoint="http://127.0.0.1:14318",
    telemetry_tempo_query_url="http://127.0.0.1:3200",
    telemetry_loki_url="http://127.0.0.1:3100",
    telemetry_prometheus_url="http://127.0.0.1:9090",
    telemetry_grafana_url="http://127.0.0.1:3003",
    lgtm_listen_host="127.0.0.1",
    lgtm_grafana_listen_host="0.0.0.0",  # noqa: S104
    lgtm_loki_port=3100,
    lgtm_loki_grpc_port=9095,
    lgtm_grafana_port=3003,
    trace_retention_days=3,
    otel_collector_metrics_port=8888,
)


# meiva:note
# The default observability values with `changes` applied.
def observability(**changes: Any) -> ObservabilityConfig:
    return replace(_DEFAULTS, **changes)


# meiva:note
# A single-box data plane: loopback Redis with an admin password and a cluster token. Both are
# fixture values, not credentials.
def data_plane(**changes: Any) -> DataPlaneInputs:
    return replace(
        DataPlaneInputs(
            db_url="postgresql://reader@127.0.0.1:5433/meiva",
            redis_url="redis://meiva:runtime@127.0.0.1:6380/0",
            redis_admin_password="abc",  # noqa: S106
            is_remote=False,
            cluster_secret="cluster-token",  # noqa: S106
        ),
        **changes,
    )


# meiva:note
# Native LGTM settings: default observability values, a gateway on 8000 with no base URL, no Grafana
# password and no Telegram bot, and fast downloads.
def lgtm_settings(**changes: Any) -> LgtmSettings:
    return replace(
        LgtmSettings(
            observability=observability(),
            webhook=AlertWebhookInputs(gateway_url="", gateway_port=8000),
            credentials=GrafanaCredentials(
                admin_password=None, telegram_bot_token="", telegram_owner_id=0
            ),
            download=FAST_DOWNLOADS,
        ),
        **changes,
    )


# meiva:note
# Collector settings: default observability values and the single-box data plane.
def collector_settings(**changes: Any) -> CollectorSettings:
    from base.deploy.release import collector_artifact

    return replace(
        CollectorSettings(
            observability=observability(),
            data_plane=data_plane(),
            download=collector_artifact.DownloadLimits(
                total_budget_s=30.0,
                socket_timeout_s=5.0,
                attempts=3,
                retry_backoff_s=0.01,
                progress_interval_s=15.0,
            ),
        ),
        **changes,
    )


# meiva:note
# Give `home` the registry record of a born gateway cluster: its start intent names the Postgres and
# Redis ports, which is where the collector's Postgres receiver finds the instance it scrapes.
def write_cluster_record(home: Path, *, postgres_port: int = 5433, redis_port: int = 6380) -> None:
    from base.cluster import INTENT_NAME, ClusterRecord

    home.mkdir(parents=True, exist_ok=True)
    record = ClusterRecord(
        ports=cast("ClusterPorts", {"postgres": postgres_port, "redis": redis_port}),
        gateway_home=str(home),
        created_at="test",
    )
    (home / INTENT_NAME).write_text(json.dumps({"record": asdict(record)}), encoding="utf-8")
