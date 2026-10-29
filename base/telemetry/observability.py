"""Cluster identity helpers shared by observability producers and readers."""

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from base.cluster.machine import MachineIdentityReader

ObservabilityEndpointVariable = Literal[
    "MEIVA_TELEMETRY_OTLP_ENDPOINT",
    "MEIVA_TELEMETRY_LOKI_URL",
]


# meiva:note
# The label a row carries when this process has not materialized its cluster yet: the
# same `.unknown` the label itself degrades to, so a reader cannot meet two spellings of
# "no cluster". `init_telemetry` materializes the real one from the home the boot hands it.
UNKNOWN_CLUSTER = ".unknown"


# meiva:note
# The home is the caller's: the boot that resolved `$MEIVA_HOME` states it here once, and
# the value is the process's own cluster identity. Reading the ambient home here would
# make a label that silently answers for whichever home the *querier* runs in.
def cluster_label(home: Path) -> str:
    from base import cluster

    try:
        return _resolve_non_empty_home_label(home)
    except Exception as exc:
        _report_label_fallback("home label", exc)
        try:
            return cluster.home_slug(home)
        except Exception as slug_exc:
            _report_label_fallback("home slug", slug_exc)
            return UNKNOWN_CLUSTER


def _resolve_non_empty_home_label(resolved_home: Path) -> str:
    from base import cluster

    label = cluster.home_label(resolved_home)
    if not label:
        raise ValueError("empty home label")
    return label


def _report_label_fallback(stage: str, exc: BaseException) -> None:
    """A degraded cluster label is reported (first + every 50th: the label is
    resolved per event), never folded in silently."""
    from base.telemetry.delivery.reporting import report_sink_failure

    report_sink_failure(f"cluster label {stage}", exc)


# meiva:note
# Whether `environ` explicitly selected an endpoint.
#
# Settings always exposes a default loopback URL, so its resolved value cannot
# distinguish operator intent from the unsafe co-located-home default. This
# narrow presence check is therefore the authority for the escape hatch.
def endpoint_override_is_explicit(
    variable: ObservabilityEndpointVariable, environ: Mapping[str, str]
) -> bool:
    return variable in environ


def gateway_observability_home(machine: MachineIdentityReader, *, home: Path) -> Path | None:
    """Return this unit's home when it serves the gateway capability.

    Missing role identity is treated as non-gateway so
    bootstrap and maintenance processes retain their historical behavior. Invalid
    configured roles propagate rather than selecting another machine posture.
    """
    from base.cluster.machine import MachineRoleMissing

    try:
        roles = machine.role()
    except MachineRoleMissing:
        return None
    return home if "gateway" in roles else None


def home_is_observability_station(
    home: Path, *, machine: MachineIdentityReader, unit_home: Path
) -> bool:
    """Whether this home is the observability station — the provider identity
    of the host's native LGTM backends.

    Two equivalent forms: the legacy ``$MEIVA_HOME/lgtm-host`` marker (operator
    designation via ``meiva lgtm on``) and the declarative ``observability-station``
    unit capability. The capability form resolves through the process's own
    machine identity and only counts when the resolved home IS this ``home`` —
    a dev worktree home never inherits the prod station's capability. Role
    identity absence (unconfigured unit, bootstrap process) falls back to
    marker-only. Invalid configured roles propagate.

    The single decision behind the converge bring-up/rendering steps, the
    backend roster gate, the producer OTLP export gate, the
    collector-lifecycle gates, and the Loki read gate — they cannot drift apart
    again (issue #622).
    """
    if (home / "lgtm-host").exists():
        return True
    from base.cluster.machine import MachineRoleMissing

    try:
        if "observability-station" not in machine.role():
            return False
    except MachineRoleMissing:
        return False
    return home.resolve() == unit_home.resolve()


def collector_allowed_for_home(
    home: Path | None, *, machine: MachineIdentityReader, unit_home: Path, endpoint_explicit: bool
) -> bool:
    """Whether the gateway home at ``home`` may run the local otel-collector.

    The observability station (``lgtm-host`` marker or ``observability-station``
    capability) always runs the sidecar. A non-station gateway runs it only when
    the operator explicitly overrode ``MEIVA_TELEMETRY_OTLP_ENDPOINT`` — the same
    escape hatch the exporter side honors
    (``base.telemetry.otlp.telemetry_otlp.observability_export_allowed``) — so an explicit
    collector export is not silently starved of its local sidecar. ``None`` (no
    gateway home: pure runner, unconfigured unit, bootstrap) keeps historical
    behavior — relay collectors are not gated.

    One decision shared by the three collector-lifecycle paths — the roster
    gate (``services.supervision.roster.spec._otel_collector_gate_reason``), the converge step
    (``cli.commands.observability.otel_collector.ensure_otel_collector_step``), and the
    sidecar healthcheck (``services.supervision.healthchecks.otel_collector.
    _collector_serves_this_home``) — so they cannot drift apart again
    (issue #622).
    """
    if home is None:
        return True
    return (
        home_is_observability_station(home, machine=machine, unit_home=unit_home)
        or endpoint_explicit
    )
