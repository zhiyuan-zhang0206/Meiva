# meiva:note
# Identity-bound service diagnostics and startup failure reporting.
#
# The canonical roster supplies a protocol probe bound to root's captured process birth.
# Missing or unobservable evidence is unavailable; a responding foreign process cannot
# certify this cluster. Root owns startup readiness and recovery. The shell probes and
# prints; the wording lives in `probe_report`.

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import NamedTuple

from base import telemetry
from base.cluster.derive import session_name
from base.daemon.health import ProbeVerdict
from cli.commands.probe_report import (
    ServiceRow,
    UnreadyWait,
    non_critical_unready_text,
    service_row_line,
    unready_text,
)
from services.supervision.roster.service_spec import ServiceSpec

__all__ = [
    "OccupiedPort",
    "ReadinessWait",
    "ServiceProbe",
    "occupied_health_ports",
    "print_non_critical_unready_services",
    "print_service_row",
    "print_unready_services",
    "probe_service",
    "report_non_critical_unready_services",
]


# meiva:note
# Operator-facing readiness evidence.
#
# `alive=None` means missing or unobservable evidence. Only `True` is a positive readiness
# claim. `terminal` identifies an occupied endpoint or unknown ownership that startup must
# resolve before launching.
class ServiceProbe(NamedTuple):
    alive: bool | None
    label: str
    detail: str
    terminal: bool = False


# meiva:note
# Report only identity-bound protocol evidence from the canonical roster.
def probe_service(spec: ServiceSpec) -> ServiceProbe:
    if spec.identity_probe is None:
        return ServiceProbe(None, "unavailable", "service has no identity-bound readiness probe")
    try:
        probe = spec.identity_probe()
    except Exception as exc:
        return ServiceProbe(
            None, "unavailable", f"identity probe raised {type(exc).__name__}: {exc}"
        )
    alive = None if probe.verdict is ProbeVerdict.UNAVAILABLE else probe.alive
    return ServiceProbe(alive, "identity", "" if alive else probe.detail, probe.terminal)


# meiva:note
# One health port this start was about to bind that someone else answers on.
#
# `detail` is the probe's own words, which already name the occupant's `$MEIVA_HOME` and the
# URL: the two facts an operator needs to decide which unit should move.
class OccupiedPort(NamedTuple):
    spec: ServiceSpec
    detail: str


# meiva:note
# Whether `spec`'s port answers a Meiva `/healthz` whose body names its home.
#
# Read off what the spec declares, not off its URL: a standard daemon carries a
# `health_name` (its URL is built from the same `ServiceEndpoints` row the daemon passes to
# `start_health_server`, so the URL probed and the port bound cannot disagree), and the gate
# declares `home_healthz` for its entry port. The gateway (`/api/health`), the browser (CDP
# `/json/version`) and the frontend declare neither: none of them serves a `/healthz` whose
# body names its home.
def _binds_a_daemon_health_port(spec: ServiceSpec) -> bool:
    return spec.health_name is not None or spec.home_healthz


# meiva:note
# The health ports among `specs` that another unit already answers on.
#
# `meiva start` probes before it binds because the alternative is worse in both directions.
# A daemon launched onto a taken port dies on `[Errno 48] Address already in use` and is
# respawned every watchdog round forever; a daemon whose port is being *relayed* from
# elsewhere is worse still, because the watchdog's probe is answered, by the wrong process,
# and the operator's `meiva start` prints a clean roster (issue #977: 402 identity-mismatch
# lines over one afternoon, each ending "manual intervention needed").
#
# Only a `terminal` verdict counts, and that is the whole discrimination: this unit's own
# daemon answers with its own home+name and is ALIVE, a stray of its own home is DOWN, and an
# empty port is DOWN, so an idempotent restart, a crashed daemon, and a cold host all pass.
# `PORT_TAKEN` is reached only by something no respawn of ours can dislodge, which is exactly
# the condition that makes launching pointless rather than merely slow.
#
# Nothing is remembered: the check is a fresh dial every start, so once the occupant leaves,
# the next start proceeds with no state to clear.
#
# Only an occupant that answers HTTP on `/healthz` is detectable. The verdict comes from
# reading a health payload, so a listener speaking any other protocol (a Postgres or redis
# on the port), one that accepts and stays silent, and an HTTP server that 404s the path all
# read DOWN, indistinguishable from an empty port, and the start proceeds into the
# `EADDRINUSE` this gate exists to prevent. The gate narrows the failure it was built for
# (issue #977's relay, which answers) and leaves the generic taken-port case where it
# already was.
def occupied_health_ports(specs: tuple[ServiceSpec, ...]) -> tuple[OccupiedPort, ...]:
    occupied: list[OccupiedPort] = []
    for spec in specs:
        if not _binds_a_daemon_health_port(spec):
            continue
        probe = probe_service(spec)
        if probe.terminal:
            occupied.append(OccupiedPort(spec, probe.detail))
    return tuple(occupied)


# meiva:note
# What the readiness wait found, and how it stopped looking.
#
# `unready` alone cannot answer the operator's first question, because the two exits below
# mean opposite things about the bound. A wait that spent 180 s on live-but-not-serving
# daemons is a case where waiting longer might have helped; a wait that returned in a second
# because every session was gone never spent the bound at all, and raising it would change
# nothing. Reporting only the specs made both print the same sentence, and on 2026-07-30
# that sentence sent the first diagnosis of a failed rollout looking for a too-tight timeout
# on a wait that had taken about a second (#1016).
#
# `sessions_gone` is the early exit, so it is a fact about every spec in `unready`; the
# deadline exit implies at least one of them was still alive (the early exit requires all of
# them to be gone). Both exits concern the CRITICAL roster only: `non_critical_unready`
# carries the demoted services that missed the current availability snapshot. They are
# reported and signalled, but never decide the exit code.
class ReadinessWait(NamedTuple):
    unready: tuple[ServiceSpec, ...]
    elapsed_s: float
    sessions_gone: bool
    non_critical_unready: tuple[ServiceSpec, ...] = ()


# meiva:note
# Name the services that never became ready, for the operator reading the same output as the
# status snapshot above it.
#
# The snapshot already shows a cross on each row; this says which crosses are the reason for
# the non-zero exit, so `rc != 0` never arrives without the what beside it. Printed AFTER the
# snapshot deliberately: an operator reads down.
def print_unready_services(wait: ReadinessWait, timeout_s: float) -> None:
    summary = UnreadyWait(
        names=tuple(session_name(s.session) for s in wait.unready),
        elapsed_s=wait.elapsed_s,
        sessions_gone=wait.sessions_gone,
    )
    print(unready_text(summary, timeout_s), file=sys.stderr)


# meiva:note
# Report optional capabilities unavailable when core startup finishes: the counterpart of
# `print_unready_services` for the tier that cannot fail the start.
def print_non_critical_unready_services(specs: tuple[ServiceSpec, ...]) -> None:
    print(non_critical_unready_text(tuple(session_name(s.session) for s in specs)), file=sys.stderr)


# meiva:note
# Emit one `service_start_unready` event per unavailable optional service.
#
# The tier's second rail: the demotion must not go silent. Every start that finds the service
# unready repeats the event; it stops once a start finds the service up.
def report_non_critical_unready_services(specs: tuple[ServiceSpec, ...]) -> None:
    for spec in specs:
        telemetry.emit(
            "telemetry",
            "service_start_unready",
            level="warning",
            source="start",
            attributes={"service": session_name(spec.session)},
        )


# meiva:note
# Probe one service and print its row of the `meiva status` table.
def print_service_row(
    spec: ServiceSpec,
    name_w: int,
    skip_reason: str | None,
    *,
    root_units: Mapping[str, Mapping[str, object]],
) -> None:
    unit = root_units.get(spec.session)
    probe = probe_service(spec)
    row = ServiceRow(
        name=session_name(spec.session),
        root_running=unit is not None and unit.get("state") == "running",
        probe_alive=probe.alive,
        probe_label=probe.label,
        probe_detail=probe.detail,
        skip_reason=skip_reason,
    )
    print(service_row_line(row, name_w))
