"""Read-only health probes for otel collector; the root supervisor owns recovery."""

import urllib.request
from collections.abc import Callable
from http.client import HTTPException
from pathlib import Path

from base.cluster.machine import MachineIdentityReader, MachineRoleMissing
from base.config import get_field
from base.daemon.health import DaemonProbe
from base.telemetry.observability import (
    collector_allowed_for_home,
    gateway_observability_home,
)
from services.supervision.healthchecks.plans.collector_metrics import (
    CollectorPressure,
    parse_pressure,
)


# meiva:note
# Whether this unit owns a collector that the healthcheck should probe. The root hands over the
# facts the answer needs: this unit's `home` and whether the operator explicitly set
# `MEIVA_TELEMETRY_OTLP_ENDPOINT`.
def collector_serves_this_home(
    *, machine: MachineIdentityReader, home: Path, endpoint_explicit: bool
) -> bool:
    try:
        roles = machine.role()
    except MachineRoleMissing:
        return False
    if "gateway" not in roles:
        return True
    return collector_allowed_for_home(
        gateway_observability_home(machine, home=home),
        machine=machine,
        unit_home=home,
        endpoint_explicit=endpoint_explicit,
    )


# meiva:note
# A valid OTLP/JSON request accepted by the local pipeline on `otlp_port` = alive.
def is_alive(otlp_port: int) -> bool:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{otlp_port}/v1/traces",
            method="POST",
            data=b'{"resourceSpans":[]}',
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.0):  # noqa: S310 — same probe
            return True
    except (OSError, HTTPException):
        return False


# meiva:note
# Observe whether the collector on `otlp_port` accepts a valid OTLP trace request.
def probe_collector_at(otlp_port: int) -> DaemonProbe:
    if is_alive(otlp_port):
        return DaemonProbe.up("collector accepts OTLP trace requests")
    return DaemonProbe.down("collector does not accept a valid OTLP trace request")


# meiva:note
# The registry's zero-argument probe: this unit's local OTLP port, read when the probe runs
# (a remote or stale producer export override cannot redirect local liveness).
def probe_collector(*, read_port: Callable[[str], int] = get_field) -> DaemonProbe:
    return probe_collector_at(int(read_port("telemetry_otlp_port")))


# meiva:note
# The collector's queue pressure from its self-metrics on `metrics_port`, or None when
# the endpoint cannot be read. The port is the per-unit one converge renders.
def queue_pressure(metrics_port: int) -> CollectorPressure | None:
    try:
        with urllib.request.urlopen(
            f"http://localhost:{metrics_port}/metrics", timeout=2.0
        ) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except (OSError, HTTPException):
        return None
    return parse_pressure(payload)
