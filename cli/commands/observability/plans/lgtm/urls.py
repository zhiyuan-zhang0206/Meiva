# meiva:note
# The URLs the rendered Grafana tree points at.
#
# Two-state on MEIVA_OBSERVABILITY_URL: empty keeps every consumer on its loopback endpoint,
# non-empty points the Loki and Prometheus datasources and the alert webhook at the remote
# observatory station. The PG datasource never follows the observatory: it is the cluster's own
# database and stays on the data plane. The shell in `lgtm_native.py` reads the settings, the
# database and the machine; these functions only write the URLs and say what to warn about.

from __future__ import annotations

from dataclasses import dataclass

from cli.commands.observability.plans.authority import host_port
from cli.commands.observability.plans.url_view import UrlView

__all__ = [
    "AlertWebhook",
    "PgDatasource",
    "alerts_webhook",
    "datasource_urls",
    "normalize_gateway_base",
    "pg_datasource",
]

_LOCAL_PG_HOSTS = ("127.0.0.1", "localhost", "::1")


# meiva:note
# The Loki and Prometheus datasource URLs: the observatory base plus each service's own port, or
# the configured read URLs without a trailing slash when no observatory is set.
def datasource_urls(
    *, observatory_base: str, loki_url: str, prometheus_url: str
) -> tuple[str, str]:
    if observatory_base:
        return f"{observatory_base}:3100", f"{observatory_base}:9090"
    return loki_url.rstrip("/"), prometheus_url.rstrip("/")


# meiva:note
# The PG datasource authority and the warning to print, if any.
@dataclass(frozen=True)
class PgDatasource:
    host_port: str
    warning: str | None


# meiva:note
# The cluster's own Postgres `host:port` for the Grafana SQL datasource.
#
# `host` and `port` are what libpq parsing of the direct database URL gave (None when absent).
# A Unix socket directory has no TCP form, so Grafana uses the local TCP listener. A database URL
# that still names loopback under a remote observatory renders a loopback datasource, which is
# only correct when Grafana runs on the Postgres host itself, so it warns.
def pg_datasource(host: str | None, port: str | None, *, remote_observatory: bool) -> PgDatasource:
    resolved_host = host or "127.0.0.1"
    if resolved_host.startswith("/"):
        resolved_host = "127.0.0.1"
    warning = None
    if remote_observatory and resolved_host in _LOCAL_PG_HOSTS:
        warning = (
            "lgtm native: MEIVA_OBSERVABILITY_URL is set but the data-plane db_url "
            f"still names {resolved_host} — the rendered PG datasource will dial the "
            "Grafana host's own loopback; point db_url at the cluster's "
            "data-plane host (task #1752) for a remote observatory."
        )
    return PgDatasource(host_port(resolved_host, port or "5432"), warning)


# meiva:note
# The gateway base URL without surrounding whitespace or a trailing slash; empty when unset.
def normalize_gateway_base(gateway_url: str) -> str:
    return gateway_url.strip().rstrip("/")


# meiva:note
# The Grafana alert webhook target and the notice to print, if any.
@dataclass(frozen=True)
class AlertWebhook:
    url: str
    notice: str | None


# meiva:note
# The Grafana alert webhook target: the gateway's own reachable address.
#
# Deliberately not derived from the observatory URL: the alert ingest endpoint lives on this
# cluster's gateway, wherever the observatory is. With no observatory the webhook is loopback on
# the gateway bind port; self-dialing a tailnet address from the gateway host can hit VPN hairpin
# filtering. A remote observatory uses the configured gateway base URL (scheme, port and optional
# proxy path included), or `machine_host` and the bind port when none is configured. A base URL
# that carries credentials, a query or a fragment is refused without echoing it.
def alerts_webhook(
    *,
    remote_observatory: bool,
    gateway_base: str,
    gateway: UrlView,
    gateway_port: int,
    machine_host: str,
) -> AlertWebhook:
    if not remote_observatory:
        return AlertWebhook(f"http://127.0.0.1:{gateway_port}/api/alerts", None)
    base = gateway_base
    if base:
        if (
            gateway.scheme not in ("http", "https")
            or not gateway.hostname
            or gateway.username is not None
            or gateway.password is not None
            or gateway.query
            or gateway.fragment
        ):
            raise ValueError("Grafana alert webhook requires a credential-free gateway base URL")
    else:
        base = f"http://{host_port(machine_host, gateway_port)}"
    notice = (
        "lgtm native: alert webhook will point at the gateway's reachable "
        f"address {base} — this presumes a REMOTE observatory "
        "Grafana consumes the rendered contact.yml (delivery lands with the "
        "observatory deployment, stage B). With a local Grafana consumer, "
        "self-dialing a tailnet address can hit VPN hairpin filtering; keep "
        "MEIVA_OBSERVABILITY_URL empty until the remote mechanism exists."
    )
    return AlertWebhook(f"{base}/api/alerts", notice)
