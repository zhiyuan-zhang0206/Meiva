# meiva:note
# Why a core service is gated out of the start roster, as a function of the facts its gate
# reads. `spec.py` collects the one fact a session's gate needs (a settings flag, a platform
# probe, a marker file) and this module judges it, so the wording of every refusal and the
# set of sessions each rule covers live in one place that needs no host to test.

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, assert_never

__all__ = [
    "BridgeFact",
    "BrowserFact",
    "CollectorFact",
    "ComputerFact",
    "FlagFact",
    "GateFact",
    "GateKind",
    "StationFact",
    "gate_kind",
    "gate_reason",
]

GateKind = Literal["flag", "browser", "computer-mcp", "station", "collector", "bridge", "none"]


# meiva:note
# A session whose only gate is one settings flag.
@dataclass(frozen=True)
class FlagFact:
    enabled: bool


# meiva:note
# The browser family: its settings flag and the platform's incapability reason (display,
# Chrome, npx), or None when the platform can run it.
@dataclass(frozen=True)
class BrowserFact:
    enabled: bool
    incapability: str | None


# meiva:note
# computer-mcp: the permissions-helper flag and the platform's incapability reason.
@dataclass(frozen=True)
class ComputerFact:
    helper_enabled: bool
    incapability: str | None


# meiva:note
# The native observability backends: whether this home is the observability station.
@dataclass(frozen=True)
class StationFact:
    is_station: bool


# meiva:note
# otel-collector: whether `collector_allowed_for_home` lets this home run the sidecar.
@dataclass(frozen=True)
class CollectorFact:
    allowed: bool


# meiva:note
# redis-bridge: the refusal text when the relay is misconfigured (None when it is not), and
# whether this host needs a relay at all.
@dataclass(frozen=True)
class BridgeFact:
    unconfigured: str | None
    has_endpoint: bool


GateFact = FlagFact | BrowserFact | ComputerFact | StationFact | CollectorFact | BridgeFact

_FLAG_REASONS = {
    "heartbeat": "disabled (MEIVA_HEARTBEAT_ENABLED off)",
    "delivery-watchdog": "disabled (MEIVA_DELIVERY_WATCHDOG_ENABLED off)",
    "im-bridge": "disabled (MEIVA_IM_BRIDGE_ENABLED off)",
}


# meiva:note
# Which gate a core session answers to; "none" is a session that is never gated.
def gate_kind(session: str) -> GateKind:
    if session in _FLAG_REASONS:
        return "flag"
    match session:
        case "browser" | "browser-mcp":
            return "browser"
        case "computer-mcp":
            return "computer-mcp"
        case "loki" | "prometheus" | "grafana":
            return "station"
        case "otel-collector":
            return "collector"
        case "redis-bridge":
            return "bridge"
        case _:
            return "none"


# meiva:note
# The reason `session` is gated out given its gate's fact, or None when it runs. A session
# with no gate takes no fact; a gated one must be given the fact of its own kind.
def gate_reason(session: str, fact: GateFact | None) -> str | None:
    if fact is None:
        if gate_kind(session) != "none":
            raise ValueError(f"session {session!r} is gated and needs its gate fact")
        return None
    match fact:
        case FlagFact(enabled=enabled):
            return _flag_reason(session, enabled=enabled)
        case BrowserFact(enabled=enabled, incapability=incapability):
            return incapability if enabled else "disabled (MEIVA_BROWSER_ENABLED off)"
        case ComputerFact(helper_enabled=helper_enabled, incapability=incapability):
            return incapability if helper_enabled else _HELPER_OFF
        case StationFact(is_station=is_station):
            return None if is_station else "this home is not an observability station"
        case CollectorFact(allowed=allowed):
            return None if allowed else _COLLECTOR_REFUSED
        case BridgeFact(unconfigured=unconfigured, has_endpoint=has_endpoint):
            return _bridge_reason(unconfigured, has_endpoint=has_endpoint)
        case _:
            assert_never(fact)


def _flag_reason(session: str, *, enabled: bool) -> str | None:
    if session not in _FLAG_REASONS:
        raise ValueError(f"session {session!r} has no settings-flag gate")
    return None if enabled else _FLAG_REASONS[session]


def _bridge_reason(unconfigured: str | None, *, has_endpoint: bool) -> str | None:
    if unconfigured is not None:
        return unconfigured
    return None if has_endpoint else "no off-box Redis ingress on this host"


_HELPER_OFF = "disabled (MEIVA_PERMISSIONS_HELPER_ENABLED off)"
_COLLECTOR_REFUSED = (
    "this gateway home is not the observability station (lgtm-host "
    "marker absent, no observability-station capability); telemetry "
    "export is unavailable; set MEIVA_TELEMETRY_OTLP_ENDPOINT to use an "
    "explicit collector"
)
