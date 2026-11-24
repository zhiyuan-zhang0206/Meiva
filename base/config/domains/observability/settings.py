"""Observability config — ObservabilitySettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by base/config.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from base.config.domains.observability.tempo import TempoSettings

# The standard OTLP/HTTP ingress port. Single source for every hardcoded 4318
# in the telemetry path: the sidecar receiver endpoint, the gateway's
# authenticated remote receiver, and the roster /
# healthcheck port probes all derive from `telemetry_otlp_port`; the
# `telemetry_otlp_endpoint` default is rendered from the same constant so the
# agent-side export target and the sidecar listener can never drift apart at
# their defaults (WP3 parameterization, task #1945).
_OTLP_INGRESS_PORT_DEFAULT = 4318


class ObservabilitySettings(TempoSettings):
    sdk_call_sampling_enabled: bool = Field(
        title="Sample SDK call events",
        default=False,
        alias="MEIVA_SDK_CALL_SAMPLING_ENABLED",
        description="Sample SDK-call events instead of recording every call. Background refresh every five seconds; execution tallies always remain complete.",
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    sdk_call_sample_every: int = Field(
        title="SDK call sampling interval",
        default=10,
        ge=1,
        alias="MEIVA_SDK_CALL_SAMPLE_EVERY",
        description="When SDK sampling is enabled, retain each event with probability 1/N. Background refresh every five seconds; 1 records every call.",
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    trace_enabled: bool = Field(
        title="Enable trace export",
        default=True,
        alias="MEIVA_TRACE_ENABLED",
        description="Export OTel spans to the local OTel Collector sidecar (which relays to Tempo). Disable to skip span export entirely — with the sidecar architecture recording IS the export (one kill switch for the whole OTLP surface).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    # meiva:note
    # The retention policy is data, not code: the maintenance job renders without any
    # `--family-days`, and reads this value at run time. A policy baked into the job
    # command travels with every release and gets silently rewritten by the next
    # converge - on 2026-11-05 an update reverted a seven-day policy to the older tiers
    # eight minutes after it was applied.
    log_family_days: str = Field(
        title="Log retention by family (days)",
        default="agent=7,shell=7,gateway=7,ops=7,watchdog=7,snapshot=7,other=7",
        alias="MEIVA_LOG_FAMILY_DAYS",
        description=(
            "Retention in days per log family, in the same FAMILY=DAYS form as "
            "`meiva logs retention --family-days`. Read by `meiva logs maintenance`, "
            "the command the daily job runs, so changing the policy is a config write "
            "rather than a release."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    log_retention_days: int = Field(
        title="Log retention (days)",
        default=14,
        ge=1,
        alias="MEIVA_LOG_RETENTION_DAYS",
        description=(
            "Delete managed top-level agent, named PTY, and rotated Loguru files "
            "older than this many days when `meiva logs retention` runs."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    trace_strip_content: bool = Field(
        title="Remove model content from traces",
        default=True,
        alias="MEIVA_TRACE_STRIP_CONTENT",
        description="Strip LLM content attributes (gen_ai.task.input/output, traceloop.entity.input/output, messages, system_instructions, ...) from recorded spans. Metadata-only spans: names, langgraph paths, checkpoint refs, agent_id, durations, status, trace_id. Content stays out of Tempo; it is fetched on demand from checkpoints by trace id.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telemetry_otlp_enabled: bool = Field(
        title="Export events through OTLP",
        default=True,
        alias="MEIVA_TELEMETRY_OTLP_ENABLED",
        description=(
            "Dual-write the unified event stream to the OTLP/HTTP backend "
            "(OTel+Tempo+Loki+Prometheus stack, 2026-08-11 decision): events -> "
            "OTLP logs (Loki), telemetry numeric payloads -> OTLP metrics "
            "(Prometheus). On (default) enables that dual-write. Off leaves the "
            "export off: OTLP export stops entirely, so Loki and Prometheus "
            "stop receiving data and their read surfaces (event reads, Grafana) "
            "freeze at the last data, and the events emitted while it is off are "
            "stored nowhere (the process log files keep their log lines). There is "
            "no Postgres or local-file fallback. Applies "
            "on the next process start."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telemetry_otlp_child_defer: bool = Field(
        title="Defer execution process telemetry export",
        default=True,
        alias="MEIVA_TELEMETRY_OTLP_CHILD_DEFER",
        description=(
            "Exec children (agent-exec) hold their OTLP export instead of bringing "
            "the exporter stack up on the first record (task #3816 M4b): records "
            "ship at clean exit, on hold saturation, or at the max-age bound. On "
            "(default) keeps child live memory flat; the local "
            "file sink stays live, so the delay (bounded by min(saturation, max-age, "
            "exit)) touches only the Loki/Prometheus arrival of a child's records. "
            "Off restores the eager first-record bring-up. Read from the environment "
            "directly by the child arm path — it cannot pull the settings singleton, "
            "which would import the config chain the deferral exists to avoid — so "
            "cluster-level overrides go through the env surface. Applies to new exec "
            "children."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    telemetry_otlp_child_defer_max_age_s: float = Field(
        title="Deferred telemetry maximum age (seconds)",
        default=60.0,
        alias="MEIVA_TELEMETRY_OTLP_CHILD_DEFER_MAX_AGE_S",
        description=(
            "Upper bound, in seconds, on how long a deferred exec child's oldest "
            "unexported record may wait before the OTLP stack is brought up "
            "mid-life and the backlog shipped (task #3816 M4b). This is the "
            "child-record observability-delay bound: Loki sees a long child's "
            "records at this age at the latest; children that exit earlier never "
            "fire it. Read from the environment directly by the child arm path."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    telemetry_otlp_endpoint: str = Field(
        title="Local OTLP collector endpoint",
        default=f"http://127.0.0.1:{_OTLP_INGRESS_PORT_DEFAULT}",
        alias="MEIVA_TELEMETRY_OTLP_ENDPOINT",
        description=(
            "Base OTLP/HTTP endpoint of the LOCAL OTel Collector sidecar "
            "(standard OTLP port, one sidecar per machine — task #1266; the "
            "default port follows `telemetry_otlp_port`). "
            "The live exporter appends /v1/logs + /v1/metrics and the trace "
            "exporter appends /v1/traces. Explicit values override producer export "
            "only; local collector health probes always use telemetry_otlp_port. "
            "Normally points at the local sidecar, "
            "on every machine — agents never dial a backend directly; the "
            "gateway sidecar fans out to loopback backends while a pure runner "
            "sidecar relays to the gateway's authenticated OTLP receiver."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    telemetry_otlp_port: int = Field(
        title="Local OTLP collector port",
        default=_OTLP_INGRESS_PORT_DEFAULT,
        alias="MEIVA_TELEMETRY_OTLP_PORT",
        ge=1,
        le=65535,
        description=(
            "TCP port of the OTLP/HTTP ingress (standard OTLP port 4318). "
            "Single source for the sidecar receiver endpoint, the gateway's "
            "authenticated remote receiver on THIS unit, "
            "and the roster/healthcheck port probes — change it here, not in "
            "the renderers. Two Meiva units on one machine must use different "
            "ports or the second sidecar cannot bind. The agent-side export "
            "target MEIVA_TELEMETRY_OTLP_ENDPOINT is a separate full-URL setting; "
            "when omitted, it follows this unit's local port."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    @property
    def harness_otlp_port(self) -> int:
        return 1024 + (self.telemetry_otlp_port - 1024 + 16_384) % (65_536 - 1024)

    @model_validator(mode="after")
    def _default_local_otlp_endpoint(self) -> Self:
        """An explicit collector URL wins; otherwise producers use this unit's listener."""
        if "telemetry_otlp_endpoint" not in self.model_fields_set:
            self.telemetry_otlp_endpoint = f"http://127.0.0.1:{self.telemetry_otlp_port}"
        return self

    gateway_otlp_endpoint: str = Field(
        title="Gateway OTLP endpoint",
        default="",
        alias="MEIVA_GATEWAY_OTLP_ENDPOINT",
        description=(
            "Authenticated gateway OTLP/HTTP ingress, derived and published by "
            "gateway bootstrap from its reachable host and local OTLP port. "
            "Pure-runner collectors and trace replay consume this projection; "
            "their own local receiver ports remain independent. Update the "
            "gateway before runners so bootstrap can publish this endpoint."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": False,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telemetry_loki_url: str = Field(
        title="Loki event history URL",
        default="http://127.0.0.1:3100",
        alias="MEIVA_TELEMETRY_LOKI_URL",
        description="Loki base URL (single-binary HTTP port) for the LGTM event-history read path — the replacement for PG `events` reads (task #1197).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telemetry_prometheus_url: str = Field(
        title="Prometheus metrics URL",
        default="http://127.0.0.1:9090",
        alias="MEIVA_TELEMETRY_PROMETHEUS_URL",
        description="Prometheus base URL (HTTP API port) for the LGTM telemetry-aggregate read path — the replacement for PG `events` llm_usage reads (task #1197).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    telemetry_grafana_url: str = Field(
        title="Grafana dashboard URL",
        default="http://127.0.0.1:3003",
        alias="MEIVA_TELEMETRY_GRAFANA_URL",
        description="Grafana base URL (HTTP port) for the LGTM dashboard and health-read path — the lgtm healthcheck's /api/health readiness probe and the Grafana-evaluated ops alerts read from the same instance (task #1789).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    lgtm_listen_host: str = Field(
        title="Loki and Prometheus listen address",
        default="127.0.0.1",
        alias="MEIVA_LGTM_LISTEN_HOST",
        description=(
            "Host/IP the native Loki, Prometheus and Tempo listeners bind to on the LGTM "
            "host (Loki HTTP+gRPC, Prometheus web API, Tempo HTTP+gRPC and OTLP/HTTP). The loopback default keeps "
            "the unauthenticated backend APIs host-local; 0.0.0.0 or a tailnet IP is "
            "the external-migration form. Rendered into the native configs and the "
            "launchd plist at converge; applies on the next LGTM restart."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_grafana_listen_host: str = Field(
        title="Grafana listen address",
        default="0.0.0.0",  # noqa: S104 — a config default value, not a bind
        alias="MEIVA_LGTM_GRAFANA_LISTEN_HOST",
        description=(
            "Host/IP Grafana's HTTP listener binds to on the LGTM host. The "
            "0.0.0.0 default preserves the historical all-interfaces bind "
            "(Grafana's own default); narrow it to 127.0.0.1 or a tailnet IP to "
            "restrict the anonymous read-only UI. Rendered into grafana.ini at "
            "converge; applies on the next Grafana restart."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_loki_port: int = Field(
        title="Loki HTTP port",
        default=3100,
        ge=1,
        le=65535,
        alias="MEIVA_LGTM_LOKI_PORT",
        description="HTTP listen port for this home's native loki backend; independent of external telemetry query URLs. Applied by LGTM converge and local health probes.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_loki_grpc_port: int = Field(
        title="Loki internal gRPC port",
        default=9095,
        ge=1,
        le=65535,
        alias="MEIVA_LGTM_LOKI_GRPC_PORT",
        description="Internal gRPC listen port for this home's single-binary Loki; use a distinct port for isolated native homes.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_prometheus_port: int = Field(
        title="Prometheus HTTP port",
        default=9090,
        ge=1,
        le=65535,
        alias="MEIVA_LGTM_PROMETHEUS_PORT",
        description="HTTP listen port for this home's native prometheus backend; independent of external telemetry query URLs. Applied by LGTM converge and local health probes.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_grafana_port: int = Field(
        title="Grafana HTTP port",
        default=3003,
        ge=1,
        le=65535,
        alias="MEIVA_LGTM_GRAFANA_PORT",
        description="HTTP listen port for this home's native grafana backend; independent of external telemetry query URLs. Applied by LGTM converge and local health probes.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_storage_dir: str = Field(
        title="Observability data directory",
        default="",
        alias="MEIVA_LGTM_STORAGE_DIR",
        description=(
            "Base directory for the native LGTM observation data on the "
            "observability-station host (Loki filesystem store, Prometheus TSDB and Tempo trace store). "
            "Empty (default) = `$MEIVA_HOME/lgtm/native/data`, byte-identical to the "
            "historical layout. A non-empty value moves the data volume to a "
            "per-machine path (e.g. a dedicated data volume), rendered into the "
            "native configs and the launchd plist at converge; applies on the next "
            "LGTM restart. Grafana's own data and the native logs stay under "
            "$MEIVA_HOME/lgtm/native regardless."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    observability_url: str = Field(
        title="Observability station URL",
        default="",
        alias="MEIVA_OBSERVABILITY_URL",
        description=(
            "Base URL (scheme://host, no port, no trailing slash) of the remote "
            "observability station. Empty (default) = every LGTM consumer stays "
            "on its current local loopback endpoint (this host's native "
            "Loki/Prometheus/Grafana). Non-empty = the two-state switch that "
            "re-renders the converge-baked consumers at the station: the Grafana "
            "Loki/Prometheus/PG datasource URLs (deploy/lgtm provisioning "
            "datasources.yml) and the gateway otel-collector's LGTM fan-out "
            "endpoints. Each consumer appends its own port (:3100 / :9090 / "
            ":5433) to this base. NOT used for the alert webhook target — that "
            "always points at this gateway's own reachable address (reachable_host), "
            "loopback 127.0.0.1 when the station is local."
        ),
        json_schema_extra={
            "help_text": "Leave empty to use local observability services; omit the port and trailing slash.",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    observability_otlp_port: int = Field(
        title="Observability station OTLP port",
        default=_OTLP_INGRESS_PORT_DEFAULT,
        ge=1,
        le=65535,
        alias="MEIVA_OBSERVABILITY_OTLP_PORT",
        description=(
            "Remote station OTLP ingress port when no pure station advertisement "
            "exists (including hybrid gateway/station units). Must match the "
            "station's MEIVA_TELEMETRY_OTLP_PORT; independent of this unit's local "
            "collector port. A matching pure station advertisement is authoritative."
        ),
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    otel_collector_metrics_port: int = Field(
        title="OpenTelemetry collector metrics port",
        default=8888,
        alias="MEIVA_OTELCOL_METRICS_PORT",
        description=(
            "TCP port where the local OTel Collector sidecar exposes its own /metrics, "
            "baked into its Prometheus pull reader at converge. Two Meiva units on one "
            "machine must use different ports or the second collector cannot start "
            "(2026-08-24 prod/preview collision)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    otel_collector_download_budget_s: float = Field(
        title="OpenTelemetry collector download budget (seconds)",
        default=900.0,
        gt=0,
        alias="MEIVA_OTELCOL_DOWNLOAD_BUDGET_S",
        description=(
            "Wall-clock budget for ONE otel-collector download operation at `meiva start`, "
            "shared by every attempt and by the waits between them; there is no separate "
            "per-attempt cap. A mirror that trickles or stalls must fail the converge step "
            "within this time instead of holding bring-up for ~30 minutes. 900 s downloads "
            "the 112 MB linux_amd64 release tarball at >=125 KB/s (measured: 112 MB in 15 s "
            "on a 7.6 MB/s link), halves the old worst case, and stays configurable for "
            "slow links (`MEIVA_OTELCOL_DOWNLOAD_BUDGET_S`)."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    otel_collector_download_socket_timeout_s: float = Field(
        title="OpenTelemetry collector download socket timeout (seconds)",
        default=30.0,
        gt=0,
        alias="MEIVA_OTELCOL_DOWNLOAD_SOCKET_TIMEOUT_S",
        description=(
            "Socket timeout of one otel-collector download request: the per-read guard that "
            "catches a half-dead connection (no data, no close). A request opens with "
            "min(this, budget remaining), so the download outlives its budget by at most one "
            "in-flight read. 30 s is long enough for a slow mirror to deliver a chunk and "
            "short enough that a wedged connection cannot hide for a whole budget."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    otel_collector_download_attempts: int = Field(
        title="OpenTelemetry collector download attempts",
        default=3,
        ge=1,
        alias="MEIVA_OTELCOL_DOWNLOAD_ATTEMPTS",
        description=(
            "Total otel-collector download executions (initial + retries) inside the one "
            "budget. Rides out a transient blip; a mirror that fails every time still ends "
            "by the budget, never by the attempt count alone."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    otel_collector_download_retry_backoff_s: float = Field(
        title="OpenTelemetry collector download retry delay (seconds)",
        default=5.0,
        ge=0,
        alias="MEIVA_OTELCOL_DOWNLOAD_RETRY_BACKOFF_S",
        description=(
            "Base retry wait of the otel-collector download: attempt N waits N x this value "
            "(5 s, 10 s for three attempts), clamped to the budget that remains so a backoff "
            "is never spent past the deadline. Spaced retries let a brief upstream hiccup "
            "clear without paying a stalled mirror's time."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    otel_collector_download_progress_interval_s: float = Field(
        title="OpenTelemetry collector download progress interval (seconds)",
        default=15.0,
        gt=0,
        alias="MEIVA_OTELCOL_DOWNLOAD_PROGRESS_INTERVAL_S",
        description=(
            "Minimum delay between two progress heartbeats of an otel-collector download. "
            "Byte counts on the converge log are what separates a slow mirror from a dead "
            "one; frequent enough to see movement, rare enough not to flood the log."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_native_download_budget_s: float = Field(
        title="Observability backend download budget (seconds)",
        default=900.0,
        gt=0,
        alias="MEIVA_LGTM_NATIVE_DOWNLOAD_BUDGET_S",
        description=(
            "Wall-clock budget for ONE native LGTM backend archive at `meiva start` (Loki, "
            "Prometheus and Grafana each get their own budget, not a shared step budget), "
            "spent by every attempt and by the waits between them; there is no per-attempt "
            "cap. Measured on a 5.8-8.7 MB/s link (2026-10-17): Loki 46 MB in 8 s, "
            "Prometheus 105 MB in 14 s, Grafana 461 MB in 53 s — so 900 s downloads the "
            "461 MB Grafana tarball at >=512 KB/s. Raise it for slow links, lower it for "
            "fast ones; the collector's separate budget is "
            "MEIVA_OTELCOL_DOWNLOAD_BUDGET_S."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_native_download_socket_timeout_s: float = Field(
        title="Observability backend download socket timeout (seconds)",
        default=30.0,
        gt=0,
        alias="MEIVA_LGTM_NATIVE_DOWNLOAD_SOCKET_TIMEOUT_S",
        description=(
            "Socket timeout of one native LGTM archive request: the per-read guard that "
            "catches a half-dead connection (no data, no close). A request opens with "
            "min(this, budget remaining), so a download outlives its budget by at most one "
            "in-flight read. 30 s is long enough for a slow mirror to deliver a chunk and "
            "short enough that a wedged connection cannot hide for a whole budget."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_native_download_attempts: int = Field(
        title="Observability backend download attempts",
        default=3,
        ge=1,
        alias="MEIVA_LGTM_NATIVE_DOWNLOAD_ATTEMPTS",
        description=(
            "Total native LGTM archive download executions (initial + retries) inside the "
            "one budget. Rides out a transient blip; a mirror that fails every time still "
            "ends by the budget, never by the attempt count alone."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_native_download_retry_backoff_s: float = Field(
        title="Observability backend download retry delay (seconds)",
        default=5.0,
        ge=0,
        alias="MEIVA_LGTM_NATIVE_DOWNLOAD_RETRY_BACKOFF_S",
        description=(
            "Base retry wait of a native LGTM archive download: attempt N waits N x this "
            "value (5 s, 10 s for three attempts), clamped to the budget that remains so a "
            "backoff is never spent past the deadline. Spaced retries let a brief upstream "
            "hiccup clear without paying a stalled mirror's time."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    lgtm_native_download_progress_interval_s: float = Field(
        title="Observability backend download progress interval (seconds)",
        default=15.0,
        gt=0,
        alias="MEIVA_LGTM_NATIVE_DOWNLOAD_PROGRESS_INTERVAL_S",
        description=(
            "Minimum delay between two progress heartbeats of a native LGTM archive "
            "download. Byte counts on the converge log are what separates a slow mirror "
            "from a dead one; frequent enough to see movement, rare enough not to flood "
            "the log."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )
