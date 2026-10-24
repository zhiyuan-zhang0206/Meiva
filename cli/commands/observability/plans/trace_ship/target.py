# meiva:note
# Where `meiva trace ship` replays the local trace mirror, chosen by the machine's role.
#
# A gateway or single-box unit sends straight to its loopback Tempo endpoint. A pure runner sends to
# the gateway collector's private-address receiver with the cluster bearer. Replay never goes
# through the local sidecar: its file exporter would mirror the replayed lines back into the mirror
# and loop the watermark.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cli.commands.observability.plans.collector.remote import gateway_ingress_endpoint
from cli.commands.observability.plans.url_parts import UrlParts

__all__ = ["OTLP_V1_PATH", "ShipTarget", "TraceShipError", "plan_ship_target"]

# meiva:note
# The standard OTLP/HTTP trace path: what Tempo's OTLP receiver listens on.
OTLP_V1_PATH = "/v1/traces"


# meiva:note
# Shipping config is incomplete or the OTLP kill switch is off.
class TraceShipError(RuntimeError): ...


# meiva:note
# The OTLP/HTTP endpoint to POST to, the extra headers it needs and the label printed for it.
@dataclass(frozen=True)
class ShipTarget:
    endpoint: str
    headers: Mapping[str, str]
    label: str


# meiva:note
# The role-correct replay target.
#
# Raises `TraceShipError` when `MEIVA_TELEMETRY_OTLP_ENABLED` is false (one kill switch for the
# whole OTLP surface, which also stops recording, so there is nothing to replay), or when a pure
# runner holds no telemetry token or the gateway has not published a usable ingress. `gateway`
# is the parsed, whitespace-stripped `gateway_endpoint`.
def plan_ship_target(
    *,
    otlp_enabled: bool,
    roles: frozenset[str],
    tempo_endpoint: str,
    gateway_endpoint: str,
    gateway: UrlParts,
    token: str | None,
) -> ShipTarget:
    if not otlp_enabled:
        raise TraceShipError(
            "MEIVA_TELEMETRY_OTLP_ENABLED is false — the whole OTLP surface "
            "(live exporter + trace ship) is off. Set it true to ship the "
            "mirror to Tempo."
        )
    if roles != frozenset({"agent-runner"}):
        return ShipTarget(
            endpoint=tempo_endpoint.rstrip("/") + OTLP_V1_PATH, headers={}, label="tempo"
        )
    if not token:
        raise TraceShipError(
            "a pure runner replays through the gateway's authenticated OTLP "
            "receiver with its capability's telemetry token, and this unit's "
            "unit holds no capability"
        )
    try:
        base = gateway_ingress_endpoint(gateway_endpoint, gateway)
    except RuntimeError as exc:
        raise TraceShipError(str(exc)) from exc
    return ShipTarget(
        endpoint=base + OTLP_V1_PATH,
        headers={"Authorization": f"Bearer {token}"},
        label="gateway OTLP relay",
    )
