# meiva:note
# What the native LGTM config templates become, and what a render warns about.
#
# The shell in `lgtm_native.py` reads the templates and the settings, validates the Loki config
# and writes the files; these functions turn template text and values into config text and
# values into warnings.

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass

from cli.commands.observability.plans.authority import (
    has_loopback_hostname,
    is_specific_non_loopback_listen,
    url_host_port,
)
from cli.commands.observability.plans.url_parts import UrlParts

__all__ = [
    "CONFIG_TEMPLATES",
    "NativeRenderValues",
    "ReadSurface",
    "env_divergences",
    "listen_read_warnings",
    "render_config",
    "render_run_script",
    "tempo_topology_warning",
]

# meiva:note
# The templates rendered into the native `config` directory, in the order they are written.
CONFIG_TEMPLATES = ("loki.yaml", "prometheus.yml", "grafana.ini", "runtime.env")


# meiva:note
# Every value the native config templates substitute.
#
# The URLs are already resolved: `loki_url`, `prometheus_url` and `pg_url` are the Grafana
# datasource endpoints, `tempo_query_url` is trimmed of its trailing slash, and the `telemetry_*`
# URLs are the configured read URLs the Prometheus scrape targets derive from.
@dataclass(frozen=True)
class NativeRenderValues:
    meiva_home: str
    storage_dir: str
    provisioning_path: str
    repo: str
    tempo_query_url: str
    loki_url: str
    prometheus_url: str
    pg_url: str
    alerts_webhook_url: str
    telemetry_loki_url: str
    telemetry_prometheus_url: str
    telemetry_grafana_url: str
    listen_host: str
    grafana_listen_host: str
    loki_port: int
    loki_grpc_port: int
    grafana_port: int


def _substitutions(values: NativeRenderValues) -> dict[str, str]:
    return {
        "MEIVA_HOME": values.meiva_home,
        "LGTM_STORAGE_DIR": values.storage_dir,
        "MEIVA_PROVISIONING_PATH": values.provisioning_path,
        "GRAFANA_PROVISIONING_PATH": f"{values.provisioning_path}/dashboards",
        "MEIVA_TEMPO_QUERY_URL": values.tempo_query_url,
        "LOKI_URL": values.loki_url,
        "PROMETHEUS_URL": values.prometheus_url,
        "PG_URL": values.pg_url,
        "ALERTS_WEBHOOK_URL": shlex.quote(values.alerts_webhook_url),
        "REPO": values.repo,
        "lgtm_grafana_listen_host": values.grafana_listen_host,
        "LGTM_LOKI_PORT": str(values.loki_port),
        "LGTM_GRAFANA_PORT": str(values.grafana_port),
    }


def _fill(template: str, substitutions: Mapping[str, str]) -> str:
    content = template
    for key, value in substitutions.items():
        content = content.replace(f"{{{{{key}}}}}", value)
    return content


# meiva:note
# One config template with its values filled in.
#
# Prometheus scrapes its own targets from the telemetry read URLs, so its template takes the
# `host:port` form of those URLs. The Loki listen host and ports ride brace-free tokens: a YAML
# plain scalar must not start with `{`, and the rendered line must stay byte-identical to the
# historical unquoted address.
def render_config(name: str, template: str, values: NativeRenderValues) -> str:
    substitutions = _substitutions(values)
    if name == "prometheus.yml":
        substitutions = {
            **substitutions,
            "MEIVA_TEMPO_QUERY_URL": url_host_port(values.tempo_query_url),
            "MEIVA_PROMETHEUS_SCRAPE_TARGET": url_host_port(values.telemetry_prometheus_url),
            "MEIVA_LOKI_SCRAPE_TARGET": url_host_port(values.telemetry_loki_url),
            "MEIVA_GRAFANA_SCRAPE_TARGET": url_host_port(values.telemetry_grafana_url),
        }
    content = _fill(template, substitutions)
    content = content.replace("__LGTM_LISTEN_HOST__", values.listen_host)
    content = content.replace("__LGTM_LOKI_PORT__", str(values.loki_port))
    return content.replace("__LGTM_LOKI_GRPC_PORT__", str(values.loki_grpc_port))


# meiva:note
# The Grafana launcher script with its values filled in.
def render_run_script(template: str, values: NativeRenderValues) -> str:
    return _fill(template, _substitutions(values))


# meiva:note
# The warning for a Tempo query URL and intake endpoint that disagree about being loopback, or
# None. A remote cluster's intake with a local query URL leaves Grafana querying the wrong Tempo.
def tempo_topology_warning(
    *, query_url: str, query: UrlParts, intake_endpoint: str, intake: UrlParts
) -> str | None:
    if has_loopback_hostname(query) == has_loopback_hostname(intake):
        return None
    return (
        "lgtm native: MEIVA_TELEMETRY_TEMPO_QUERY_URL resolves to "
        f"{query_url} but the Tempo intake endpoint is {intake_endpoint} "
        "— set MEIVA_TELEMETRY_TEMPO_QUERY_URL to the remote cluster's query URL"
    )


# meiva:note
# One backend's listen host and the read URL consumers use to reach it.
@dataclass(frozen=True)
class ReadSurface:
    backend: str
    env_var: str
    listen_host: str
    read_url: str
    read: UrlParts


# meiva:note
# One warning per backend that listens on a specific non-loopback address while its read URL
# still resolves to loopback: the Prometheus scrape targets and the consumers' reads use the read
# URLs, so a widened listen host with a loopback read URL breaks those paths.
def listen_read_warnings(surfaces: tuple[ReadSurface, ...]) -> tuple[str, ...]:
    return tuple(
        "lgtm native: "
        f"{surface.env_var} resolves to {surface.read_url} but {surface.backend} listens on "
        f"{surface.listen_host} — set {surface.env_var} to the listen address or keep the "
        "listen host loopback"
        for surface in surfaces
        if is_specific_non_loopback_listen(surface.listen_host)
        and has_loopback_hostname(surface.read)
    )


# meiva:note
# One warning per rendered value that disagrees with what the unit's `.env` declares.
#
# Settings give an inherited environment value precedence over the file for host-scope keys, and
# a long-lived parent forwards its snapshot to every child, so a converge run inside such a
# session bakes the stale value into the rendered tree silently. A key the file does not declare
# is skipped: environment-only supply is legitimate.
def env_divergences(
    declared: Mapping[str, str | None], values: Mapping[str, str], *, env_file: str
) -> tuple[str, ...]:
    warnings: list[str] = []
    for alias, value in values.items():
        file_value = declared.get(alias)
        if file_value is None or file_value.rstrip("/") == value.rstrip("/"):
            continue
        warnings.append(
            f"lgtm native: {alias} resolved to {value} but {env_file} declares "
            f"{file_value} — an inherited environment value is in effect"
        )
    return tuple(warnings)
