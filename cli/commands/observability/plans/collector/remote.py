# meiva:note
# The bearer-authenticated OTLP surface between a cluster's collectors.
#
# A gateway's sidecar accepts pure-runner relays and an observability-station sidecar accepts
# remote gateway collectors, both through `otlp/remote` and `bearertokenauth/cluster` with the
# cluster's telemetry token. These functions validate the published ingress endpoint, name the
# bearer and write the receiver fragments; the shell in `otel_collector.py` reads the token, the
# machine identity and the port.

from __future__ import annotations

from dataclasses import dataclass

from base.host.net.predicates import is_loopback_host
from cli.commands.observability.plans.authority import host_port, is_unspecified_address
from cli.commands.observability.plans.collector.text import yaml_quote
from cli.commands.observability.plans.url_parts import UrlParts

__all__ = [
    "NO_REMOTE_INGRESS",
    "RemoteFragments",
    "cluster_bearer",
    "gateway_ingress_endpoint",
    "ingress_endpoint_problem",
    "remote_receiver_fragments",
]

_REMOTE_TRACE_PIPELINE = """
    # A remote peer already writes its own durable trace mirror. The remote
    # pipeline therefore fans out only to Tempo; adding file/traces here
    # duplicates every remote span in the local mirror and makes replay
    # provenance ambiguous.
    traces/remote:
      receivers: [otlp/remote]
      processors: [memory_limiter, batch]
      exporters: [otlphttp/tempo]
"""


# meiva:note
# Why `endpoint` cannot serve as the published gateway OTLP ingress, or None.
#
# The one validity definition for `MEIVA_GATEWAY_OTLP_ENDPOINT`: a non-loopback `http://host:port`
# with no credentials, path, query or fragment. `endpoint` is the parsed, whitespace-stripped
# value.
def ingress_endpoint_problem(endpoint: UrlParts) -> str | None:
    host = endpoint.hostname or ""
    if (
        endpoint.scheme != "http"
        or not host
        or is_loopback_host(host)
        or is_unspecified_address(host)
        or endpoint.port is None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.path not in ("", "/")
        or endpoint.query
        or endpoint.fragment
    ):
        return (
            "the published MEIVA_GATEWAY_OTLP_ENDPOINT must be a non-loopback "
            "http://host:port ingress with no credentials or path"
        )
    return None


# meiva:note
# The authenticated gateway ingress published by bootstrap, without a trailing slash.
#
# `raw` is the configured value and `parsed` its stripped, parsed form. An invalid value fails
# closed: the runner must wait for the gateway to publish a usable ingress.
def gateway_ingress_endpoint(raw: str, parsed: UrlParts) -> str:
    if ingress_endpoint_problem(parsed) is not None:
        raise RuntimeError(
            "cannot build runner OTLP relay: gateway bootstrap must publish "
            "MEIVA_GATEWAY_OTLP_ENDPOINT as its non-loopback HTTP ingress with "
            "an explicit port; update the gateway first, then restart this runner"
        )
    return raw.strip().rstrip("/")


# meiva:note
# The `Authorization` header value for the cluster's telemetry token.
#
# A split topology that cannot authenticate its relay fails closed: remote ingress must never be
# unauthenticated.
def cluster_bearer(token: str | None) -> str:
    if not token:
        raise RuntimeError(
            "cannot build split-cluster OTLP relay without a telemetry token (the "
            "gateway's MEIVA_CLUSTER_SECRET, or an API-bearing unit capability); "
            "remote ingress must fail closed"
        )
    return f"Bearer {token}"


# meiva:note
# The template fragments for the remote OTLP ingress.
@dataclass(frozen=True)
class RemoteFragments:
    receiver: str
    auth_extension: str
    auth_service_extension: str
    pipeline_receiver: str
    trace_pipeline: str


NO_REMOTE_INGRESS = RemoteFragments("", "", "", "", "")


# meiva:note
# The fragments that expose the authenticated remote ingress, or none.
#
# Remote ingress exists only when the unit could actually have remote peers: no telemetry token
# (the unit holds neither the secret nor a capability) and a loopback reachable host (co-located
# posture) both mean no remote peers, so nothing is rendered. A wildcard reachable host is a
# configuration error either way and fails closed, whichever roles the unit carries. Any gateway-
# or station-capable host with a token and a non-loopback address may serve remote peers,
# including a hybrid gateway, runner and station such as production.
def remote_receiver_fragments(
    roles: frozenset[str] | None, *, host: str, token: str | None, ingress_port: int
) -> RemoteFragments:
    if roles is None or not (roles & {"gateway", "observability-station"}):
        return NO_REMOTE_INGRESS
    if is_unspecified_address(host):
        raise RuntimeError(
            "cannot expose authenticated OTLP ingress: reachable host is "
            "wildcard; set MEIVA_MACHINE_HOST to this host's exact "
            "private address"
        )
    if not token or is_loopback_host(host):
        return NO_REMOTE_INGRESS
    return RemoteFragments(
        receiver=f"""
  otlp/remote:
    protocols:
      http:
        endpoint: {yaml_quote(host_port(host, ingress_port))}
        auth:
          authenticator: bearertokenauth/cluster
""",
        auth_extension=f"""
  bearertokenauth/cluster:
    token: {yaml_quote(token)}
""",
        auth_service_extension=", bearertokenauth/cluster",
        pipeline_receiver=", otlp/remote",
        trace_pipeline=_REMOTE_TRACE_PIPELINE,
    )
