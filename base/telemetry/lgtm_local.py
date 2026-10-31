"""Local native LGTM listener identity, independent of external query URLs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# meiva:note
# The observability fields this module reads: the two listen hosts, the three loopback ports the
# probes dial, and the retention directory. A composition root reads them once (`from_fields`)
# and hands the value down, so nothing here names the process settings.
@dataclass(frozen=True)
class LgtmSettings:
    loki_port: int
    prometheus_port: int
    grafana_port: int
    listen_host: str
    grafana_listen_host: str
    storage_dir_setting: str

    @classmethod
    def from_fields(cls, read_field: Callable[[str], Any]) -> LgtmSettings:
        return cls(
            loki_port=int(read_field("lgtm_loki_port")),
            prometheus_port=int(read_field("lgtm_prometheus_port")),
            grafana_port=int(read_field("lgtm_grafana_port")),
            listen_host=str(read_field("lgtm_listen_host")),
            grafana_listen_host=str(read_field("lgtm_grafana_listen_host")),
            storage_dir_setting=str(read_field("lgtm_storage_dir")),
        )


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
            "--storage.tsdb.retention.time=180h",
            "--storage.tsdb.retention.size=8GB",
            "--web.enable-otlp-receiver",
            "--web.listen-address={lgtm_listen_host}:{lgtm_prometheus_port}",
        ),
        gomemlimit="1GiB",
        archive_member="prometheus-3.13.2.darwin-arm64/prometheus",
        binary_path="bin/prometheus",
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
    "grafana": "/api/health",
}


def binary_path(home: Path, name: str) -> Path:
    """The exact installed executable owned by this home."""
    relative = {
        "loki": "bin/loki",
        "prometheus": "bin/prometheus",
        "grafana": "grafana-home/bin/grafana",
    }[name]
    return (home / "lgtm/native" / relative).resolve()


def backend_urls(observability: LgtmSettings) -> dict[str, str]:
    """Probe the configured local binds; wildcard listeners are dialed on loopback."""
    ports = {
        "loki": observability.loki_port,
        "prometheus": observability.prometheus_port,
        "grafana": observability.grafana_port,
    }
    urls: dict[str, str] = {}
    for name, port in ports.items():
        host = observability.grafana_listen_host if name == "grafana" else observability.listen_host
        host = host.removeprefix("[").removesuffix("]")
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)  # noqa: S104 — wildcard classification, never a bind
        urls[name] = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    return urls


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
