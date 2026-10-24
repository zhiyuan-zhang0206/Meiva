# meiva:note
# Which services the health probe may certify, and which of them failed.
#
# The probe reuses the `meiva status` roster and the ownership-bound per-service probe.
# Unknown evidence or an unresolved roster cannot certify health, even when the gateway
# responds. The shell reads the roles, the last start's launch record and the roster, probes
# the sessions this module names, and hands the answers back for judgment.
#
# The probe's `detail` rides along into the failing entry because the list becomes the
# `health_probe_failing` message, the only diagnostic a human sees in the alert. "meiva-ops
# not responding" and "meiva-ops is answering, but it is /home/meiva/.meiva" are the same
# bare session name and completely different incidents: the second one means another unit
# holds this unit's port and no amount of waiting fixes it.

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

__all__ = [
    "ROSTER_UNAVAILABLE",
    "LaunchRecord",
    "LaunchUnreadable",
    "Launched",
    "NeverLaunched",
    "ProbeOutcome",
    "RosterGate",
    "Unavailable",
    "bridge_failure",
    "expects_redis_bridge",
    "failing_services",
    "launch_scope",
    "sessions_to_probe",
]

ROSTER_UNAVAILABLE = "service roster unavailable"


# meiva:note
# The services the last `meiva start` put in this home's root manifest.
@dataclass(frozen=True)
class Launched:
    sessions: frozenset[str]


# meiva:note
# No start has launched this unit.
@dataclass(frozen=True)
class NeverLaunched: ...


# meiva:note
# The launch record exists but could not be read.
@dataclass(frozen=True)
class LaunchUnreadable:
    detail: str


LaunchRecord = Launched | NeverLaunched | LaunchUnreadable


# meiva:note
# One service of this host's roster and the reason the capability gate drops it (None when it
# would run).
@dataclass(frozen=True)
class RosterGate:
    session: str
    gate_reason: str | None


# meiva:note
# What probing one session found: `alive` is True / False for positive / negative evidence
# and None for missing or unobservable evidence, which never counts as healthy.
@dataclass(frozen=True)
class ProbeOutcome:
    session: str
    alive: bool | None
    detail: str


# meiva:note
# The services cannot be certified at all; `reason` becomes the failing entry.
@dataclass(frozen=True)
class Unavailable:
    reason: str


# meiva:note
# The launch record when it can scope the probe, else why nothing can be certified. A unit no
# start has launched, or whose record cannot be read, has no inventory to hold the gateway's
# answer against.
def launch_scope(launched: LaunchRecord) -> Launched | Unavailable:
    if isinstance(launched, LaunchUnreadable):
        return Unavailable(f"launched services unavailable ({launched.detail})")
    if isinstance(launched, NeverLaunched):
        return Unavailable("launched services unavailable (no start has launched this unit)")
    return launched


# meiva:note
# The roster's sessions to probe, in roster order: those the capability gate keeps AND the
# last start launched. A gated-out service is intentionally absent, and a service no start
# launched is not this unit's to certify.
def sessions_to_probe(launched: Launched, roster: Sequence[RosterGate]) -> tuple[str, ...]:
    return tuple(
        entry.session
        for entry in roster
        if entry.gate_reason is None and entry.session in launched.sessions
    )


# meiva:note
# The failing services, each annotated with why when the probe said.
def failing_services(outcomes: Sequence[ProbeOutcome]) -> tuple[str, ...]:
    return tuple(
        f"{outcome.session} ({outcome.detail})" if outcome.detail else outcome.session
        for outcome in outcomes
        if outcome.alive is not True
    )


# meiva:note
# The Redis bridge is a gateway-host listener; a host whose role is unresolved or that does
# not serve the gateway has nothing to certify.
def expects_redis_bridge(roles: Collection[str] | None) -> bool:
    return roles is not None and "gateway" in roles


# meiva:note
# End-to-end Redis relay failure text, or None when healthy or not required.
#
# Alert-only: a failed host listener is an infrastructure outage, not evidence that rolling
# application code back is safe or useful. The probe authenticates and PINGs through the
# off-box endpoint; an open TCP port alone cannot certify the forwarding path.
def bridge_failure(
    *, required: bool, serving: bool, endpoint: str | None, detail: str
) -> str | None:
    if not required or serving:
        return None
    where = f" {endpoint}" if endpoint else ""
    return f"Redis bridge{where} failed authenticated PING ({detail})"
