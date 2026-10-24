# meiva:note
# Where a collector sends what it receives, and which URLs that fan-out is built from.
#
# Three shapes (docs/conventions/data/reachability-and-credentials.md): a pure agent-runner relays
# every signal to its gateway's authenticated OTLP ingress; a unit that consumes a remote
# observatory relays every signal to the station's single bearer-authenticated ingress and never
# dials the station's loopback-bound native backends; everything else fans out to its local
# backends. The shell in `otel_collector.py` resolves the endpoints and the bearer.

from __future__ import annotations

from enum import Enum

from cli.commands.observability.plans.collector.exporter_blocks import (
    BACKEND_EXPORTERS,
    RELAY_EXPORTERS,
)
from cli.commands.observability.plans.collector.text import yaml_quote

__all__ = ["ExporterShape", "exporter_shape", "fanout_bases", "render_exporters"]


class ExporterShape(Enum):
    RUNNER_RELAY = "runner-relay"
    STATION_RELAY = "station-relay"
    LOCAL_BACKENDS = "local-backends"


# meiva:note
# The fan-out shape of a unit with these roles.
#
# `observability_url_set` is the raw setting: a malformed value still selects the station relay,
# which then refuses it rather than silently dialing local backends.
def exporter_shape(roles: frozenset[str] | None, *, observability_url_set: bool) -> ExporterShape:
    if roles == frozenset({"agent-runner"}):
        return ExporterShape.RUNNER_RELAY
    if observability_url_set:
        return ExporterShape.STATION_RELAY
    return ExporterShape.LOCAL_BACKENDS


# meiva:note
# The gateway collector's Loki and Prometheus fan-out base URLs.
#
# With a remote observatory (`station_endpoint` set, and `remote` true) every signal enters the
# station through its ONE bearer-authenticated OTLP ingress, so both bases are that endpoint.
# Otherwise the per-service settings URLs are used, whose defaults are this host's loopback
# backends. The runner relay path is unaffected: it always relays to the gateway collector.
def fanout_bases(
    *, station_endpoint: str | None, loki_url: str, prometheus_url: str
) -> tuple[str, str]:
    if station_endpoint is not None:
        return station_endpoint, station_endpoint
    return loki_url.rstrip("/") + "/otlp", prometheus_url.rstrip("/") + "/api/v1/otlp"


# meiva:note
# The `exporters:` block for a shape. A relay shape carries its `relay_endpoint` and
# `authorization` header; the local shape carries the Tempo endpoint and the two fan-out bases.
def render_exporters(
    shape: ExporterShape,
    *,
    relay_endpoint: str,
    authorization: str,
    tempo_endpoint: str,
    bases: tuple[str, str],
) -> str:
    if shape is ExporterShape.LOCAL_BACKENDS:
        loki_base, prom_base = bases
        return BACKEND_EXPORTERS.format(
            tempo_endpoint=yaml_quote(tempo_endpoint.rstrip("/")),
            loki_base=yaml_quote(loki_base),
            prom_base=yaml_quote(prom_base),
        )
    return RELAY_EXPORTERS.format(
        endpoint=yaml_quote(relay_endpoint), authorization=yaml_quote(authorization)
    )
