"""Native LGTM service commands, listener addresses and retained storage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from base.cluster.listener_plan import TcpBind


# meiva:note
# The native listeners and retained data volume, resolved from observability fields. A composition root reads them once (`from_fields`)
# and hands the value down, so nothing here names the process settings.
@dataclass(frozen=True)
class LgtmSettings:
    loki_port: int
    prometheus_port: int
    grafana_port: int
    listen_host: str
    grafana_listen_host: str
    storage_dir_setting: str
    loki_grpc_port: int
    tempo_query_url: str
    tempo_intake_url: str
    tempo_grpc_port: int

    @classmethod
    def from_fields(cls, read_field: Callable[[str], object]) -> LgtmSettings:
        return cls(
            loki_port=_port(read_field("lgtm_loki_port")),
            prometheus_port=_port(read_field("lgtm_prometheus_port")),
            grafana_port=_port(read_field("lgtm_grafana_port")),
            listen_host=str(read_field("lgtm_listen_host")),
            grafana_listen_host=str(read_field("lgtm_grafana_listen_host")),
            storage_dir_setting=str(read_field("lgtm_storage_dir")),
            loki_grpc_port=_port(read_field("lgtm_loki_grpc_port")),
            tempo_query_url=str(read_field("telemetry_tempo_query_url")),
            tempo_intake_url=str(read_field("telemetry_tempo_endpoint")),
            tempo_grpc_port=_port(read_field("lgtm_tempo_grpc_port")),
        )

    @property
    def tempo(self) -> TempoListeners:
        return TempoListeners.from_urls(
            query_url=self.tempo_query_url,
            intake_url=self.tempo_intake_url,
            grpc_port=self.tempo_grpc_port,
        )


# meiva:note
# A configured port as an int: a number or numeric text.
def _port(value: object) -> int:
    if not isinstance(value, int | str):
        raise TypeError(f"an LGTM port must be a number, got {type(value).__name__}")
    return int(value)


@dataclass(frozen=True)
class TempoListeners:
    http: int
    otlp: int
    grpc: int

    @classmethod
    def from_urls(cls, *, query_url: str, intake_url: str, grpc_port: int) -> TempoListeners:
        return cls(http=_http_port(query_url), otlp=_http_port(intake_url), grpc=grpc_port)

    def ports(self) -> tuple[int, int, int]:
        return self.http, self.otlp, self.grpc


def _http_port(url: str) -> int:
    parts = urlsplit(url)
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme == "https" else 80


@dataclass(frozen=True)
class _NativeService:
    arguments: tuple[str, ...]
    gomemlimit: str | None
    archive_member: str | None
    binary_path: str
    uses_run_script: bool = False


NATIVE_SERVICES: dict[str, _NativeService] = {
    "loki": _NativeService(
        arguments=("-config.file={config}/loki.yaml",),
        gomemlimit="2GiB",
        archive_member="loki-darwin-arm64",
        binary_path="bin/loki",
    ),
    "prometheus": _NativeService(
        arguments=(
            "--config.file={config}/prometheus.yml",
            "--storage.tsdb.path={data}/prom",
            "--storage.tsdb.retention.time=7d",
            "--storage.tsdb.retention.size=8GB",
            "--web.enable-otlp-receiver",
            "--web.listen-address={lgtm_listen_host}:{lgtm_prometheus_port}",
        ),
        gomemlimit="1GiB",
        archive_member="prometheus-3.13.2.darwin-arm64/prometheus",
        binary_path="bin/prometheus",
    ),
    "tempo": _NativeService(
        arguments=("-config.file={config}/tempo.yaml",),
        gomemlimit="600MiB",
        archive_member="tempo",
        binary_path="bin/tempo",
    ),
    "grafana": _NativeService(
        arguments=(
            "server",
            "--config={config}/grafana.ini",
            "--homepath={homepath}",
        ),
        gomemlimit=None,
        archive_member=None,
        binary_path="grafana-home/bin/grafana",
        uses_run_script=True,
    ),
}


BACKENDS = tuple(NATIVE_SERVICES)

# Each backend's own supported health/readiness path. Local lifecycle probes
# must use these, NOT the root URL: Grafana's root redirects to the public
# Gateway root (serve_from_sub_path), and following that redirect while the
# Gateway is still waiting on the same start() makes a healthy Grafana read
# as down and gets it restarted (#2047).
HEALTH_PATHS = {
    "loki": "/ready",
    "prometheus": "/-/ready",
    "tempo": "/ready",
    "grafana": "/api/health",
}


def binary_path(home: Path, name: str) -> Path:
    """The exact installed executable owned by this home."""
    relative = NATIVE_SERVICES[name].binary_path
    return (home / "lgtm/native" / relative).resolve()


def backend_binds(name: str, observability: LgtmSettings) -> tuple[TcpBind, ...]:
    host = observability.listen_host
    if name == "tempo":
        return tuple(TcpBind(host, port) for port in observability.tempo.ports())
    if name == "loki":
        return TcpBind(host, observability.loki_port), TcpBind(host, observability.loki_grpc_port)
    if name == "grafana":
        return (TcpBind(observability.grafana_listen_host, observability.grafana_port),)
    return (TcpBind(host, observability.prometheus_port),)


def backend_url(bind: TcpBind) -> str:
    host = bind.host.removeprefix("[").removesuffix("]")
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)  # noqa: S104
    return f"http://{TcpBind(host, bind.port)}"


def backend_urls(observability: LgtmSettings) -> dict[str, str]:
    """URLs for the backends whose ports are already available as settings integers."""
    return {
        name: backend_url(backend_binds(name, observability)[0])
        for name in BACKENDS
        if name != "tempo"
    }


def storage_dir(home: Path, observability: LgtmSettings) -> Path:
    """The explicit local observation data volume, retained across service stops."""
    configured = observability.storage_dir_setting.strip()
    return Path(configured).expanduser().resolve() if configured else home / "lgtm/native/data"


def service_argv(home: Path, name: str, observability: LgtmSettings) -> tuple[str, ...]:
    """One native invocation, launched only as an meiva-root unit."""
    native = home.resolve() / "lgtm/native"
    spec = NATIVE_SERVICES[name]
    if spec.uses_run_script:
        return (str(native / "grafana/run.sh"),)
    substitutions = {
        "config": str(native / "config"),
        "data": str(storage_dir(home, observability)),
        "homepath": str(native / "grafana-home"),
        "lgtm_listen_host": observability.listen_host,
        "lgtm_prometheus_port": str(observability.prometheus_port),
    }
    return (str(binary_path(home, name)), *(s.format(**substitutions) for s in spec.arguments))


def service_input_paths(home: Path, name: str) -> tuple[Path, ...]:
    """Declare the complete configuration tree; durable data is not a birth input."""
    native = home / "lgtm/native"
    paths = [native / f"version-{name}", native / f"platform-{name}", native / "config"]
    if name == "grafana":
        paths.extend([native / "grafana/run.sh", native / "grafana/admin_password"])
    return tuple(paths)


def service_environment(name: str) -> dict[str, str]:
    """Process environment independent of the manifest's typed input seals."""
    limit = NATIVE_SERVICES[name].gomemlimit
    return {} if limit is None else {"GOMEMLIMIT": limit}
