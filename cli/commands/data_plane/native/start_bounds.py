# meiva:note
# The bounds of one data-plane start, and the bounded bind wait they gate.
#
# A data-plane listener can only bind an address the kernel already has on an interface. On reboot
# brew / launchd can start the data plane before the private network has assigned
# `MEIVA_MACHINE_HOST`, so binding it fails with EADDRNOTAVAIL and the autostart dies; the wait
# below is what both halves (Postgres and Redis) use instead of binding and hoping.
#
# `StartTuning` is the one object a start reads its bounds from: production uses
# `PRODUCTION_TUNING`, a test passes small values.


from __future__ import annotations

import socket
import sys
from dataclasses import dataclass

from base.clock import Timers
from base.cluster import port_preflight
from base.cluster.machine import MachineIdentityReader
from cli.commands.data_plane.instance import pg_plan
from cli.commands.data_plane.instance.bounded_wait import Wait, wait_verdict
from cli.commands.data_plane.pg_probe import PG_PROBE_TIMEOUT_S

# meiva:note
# Bounded wait for this host's non-loopback bind address (MEIVA_MACHINE_HOST) to appear on a local
# interface before the Postgres data plane binds to it. On reboot brew / launchd can start `meiva`
# before the private network has assigned the address, so binding it fails and the whole
# autostart dies.
BIND_WAIT_TIMEOUT_S = 60.0
_BIND_WAIT_INTERVAL_S = 2.0

# meiva:note
# How long a postmaster may take to answer, counted from its spawn (the pg half's default).
PG_START_TIMEOUT_S = 60.0


# meiva:note
# The bounds of one start, each defaulting to its production constant: how long the reachable bind
# address may take to appear, how long a postmaster may take to answer, how long one readiness
# probe may take. A test passes small ones; nothing else sets them.
@dataclass(frozen=True)
class StartTuning:
    bind_wait_s: float = BIND_WAIT_TIMEOUT_S
    pg_start_s: float = PG_START_TIMEOUT_S
    pg_probe_s: float = PG_PROBE_TIMEOUT_S


PRODUCTION_TUNING = StartTuning()


# meiva:note
# True if `addr` is currently assigned to a local interface (bindable).
#
# A Postgres or PgBouncer listener can only bind an address the kernel has on an interface;
# before the private-network interface comes up, binding its address fails with EADDRNOTAVAIL.
# Probing with a throwaway bind is the portable check.
def addr_assigned(addr: str) -> bool:
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((addr, 0))
        return True
    except OSError:
        return False


# meiva:note
# Block (bounded by `timeout_s`) until the host's non-loopback bind address is assigned to a
# local interface.
#
# Returns True immediately on a loopback-only (single-box) host — loopback is always present.
# Returns False on timeout so the caller fails fast with a clear message instead of the
# daemon dying on an un-bindable address.
def wait_for_reachable_bind(*, host: str, timeout_s: float, timers: Timers) -> bool:
    if host in port_preflight.LOOPBACK_ALIASES:
        return True
    started = timers.monotonic()
    while True:
        verdict = wait_verdict(
            satisfied=addr_assigned(host),
            elapsed_s=timers.monotonic() - started,
            timeout_s=timeout_s,
        )
        if verdict is Wait.DONE:
            return True
        if verdict is Wait.EXPIRED:
            return False
        timers.sleep(_BIND_WAIT_INTERVAL_S)


# meiva:note
# Wait for `host` to be assigned to an interface; print why and say no when it never appears.
def await_bind(service: str, host: str, tuning: StartTuning, *, timers: Timers) -> bool:
    if wait_for_reachable_bind(host=host, timeout_s=tuning.bind_wait_s, timers=timers):
        return True
    print(pg_plan.bind_refusal(service, host, int(tuning.bind_wait_s)), file=sys.stderr)
    return False


# meiva:note
# The machine's declared reachable address, or None on a single box that binds loopback alone.
def off_box_host(machine: MachineIdentityReader) -> str | None:
    host = machine.host()
    return None if host in port_preflight.LOOPBACK_ALIASES else host
