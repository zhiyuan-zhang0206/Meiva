# meiva:note
# The gateway's own cluster-status snapshot, as `meiva status` prints it.
#
# The gateway can only see itself, and a gateway that is down or unreachable is itself a
# status signal: the shell turns every outcome into one of these values (the report, or the
# reason there is none) and this module renders it inline, so the local probes that already
# ran are never lost to a gateway failure.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "GatewayCredentialMissing",
    "GatewayOutcome",
    "GatewayReport",
    "GatewayUnreachable",
    "GatewayUnresolved",
    "gateway_status_lines",
]


# meiva:note
# What the gateway reports about itself (`GET /api/cluster/status`).
@dataclass(frozen=True)
class GatewayReport:
    machine_name: str
    # meiva:note
    # The capability label ("gateway + agent-runner", "none", ...), already formatted.
    serves: str
    paused: bool
    paused_reason: str | None


# meiva:note
# The request was made and the gateway did not answer it with a status body.
@dataclass(frozen=True)
class GatewayUnreachable:
    detail: str


# meiva:note
# The gateway URL could not even be formed (unset gateway URL or machine role).
@dataclass(frozen=True)
class GatewayUnresolved:
    detail: str


# meiva:note
# This process holds nothing to present to the gateway: no machine API token and no cluster
# secret, and the cluster API always authenticates. `detail` is the credential policy's own
# sentence, which says what is missing and where to run the command from.
@dataclass(frozen=True)
class GatewayCredentialMissing:
    detail: str


GatewayOutcome = GatewayReport | GatewayUnreachable | GatewayUnresolved | GatewayCredentialMissing


def gateway_status_lines(outcome: GatewayOutcome) -> tuple[str, ...]:
    heading = ("", "gateway cluster status (GET /api/cluster/status):")
    if isinstance(outcome, GatewayUnreachable):
        return (*heading, f"  ✗ gateway unreachable: {outcome.detail}")
    if isinstance(outcome, GatewayUnresolved):
        return (*heading, f"  ✗ cannot resolve gateway URL: {outcome.detail}")
    if isinstance(outcome, GatewayCredentialMissing):
        return (*heading, f"  ✗ cannot authenticate to the gateway: {outcome.detail}")
    paused_note = f" ({outcome.paused_reason})" if outcome.paused_reason else ""
    return (
        *heading,
        f"  machine_name: {outcome.machine_name}",
        f"  serves:       {outcome.serves}",
        f"  paused:       {outcome.paused}{paused_note}",
    )
