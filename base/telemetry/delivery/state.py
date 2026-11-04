"""The per-process emitter binding: pipeline, process name, agent, machine and cluster."""

from __future__ import annotations

import socket
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Protocol, TypedDict

from base.telemetry.delivery.receipts import DrainResult, Event
from base.telemetry.delivery.reporting import report_sink_failure


def _hostname() -> str:
    return socket.gethostname()


# meiva:note
# `$MEIVA_HOME/.env` is the single source of truth for a machine's name, and a process can reach
# that home without the file having been loaded into its environment (another user's shell on a
# shared host, a service started outside its unit's environment). The read lives here, next to
# the binding that needs it, so the identity is available in exactly the case the environment
# cannot answer.
def _home_machine_name(home: Path) -> str:
    try:
        text = (home / ".env").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "MEIVA_MACHINE_NAME":
            return value.strip().strip('"').strip("'")
    return ""


# meiva:note
# The hostname this process observed, for the diagnostic column only: it answers "which box
# wrote this row" without ever standing in for the machine's identity.
def machine_hostname() -> str:
    return _hostname()


# meiva:note
# Resolve this row's machine dimension: a canonical name, or `unknown`.
#
# The identity comes from the process's own reader first, then from `home/.env` -- the file that
# records this machine's name and that a process can reach even when its environment never
# loaded it (another user's shell on a shared host, a service started outside its unit's
# environment). When neither answers the value is `unknown` and the substitution is announced
# once. It is never the hostname: a hostname is a different identity form, and writing it here
# made one host count as two in every reader that grouped by `machine`, silently. The hostname
# still reaches the data as the diagnostic value `machine_hostname`.
#
# A failure other than the documented absence (`MachineNameMissing`) propagates rather than
# pinning the owner to a wrong dimension.
def resolve_machine(
    name_reader: Callable[[], str] | None = None, *, home: Path | None = None
) -> str:
    from base.cluster.machine import MachineNameMissing
    from base.telemetry.machine import canonical_machine

    sources: list[tuple[str, Callable[[], str]]] = []
    if name_reader is not None:
        sources.append(("the identity reader", name_reader))
    if home is not None:
        sources.append(("the home environment file", partial(_home_machine_name, home)))
    for _label, read in sources:
        try:
            raw = read()
        except MachineNameMissing:
            continue
        if raw.strip():
            return canonical_machine(raw).value
    return _announce_unknown_machine([label for label, _ in sources])


# meiva:note
# One line per process, not per row: a service that lost its identity writes thousands of rows
# and the operator needs to learn it once.
def _announce_unknown_machine(tried: list[str]) -> str:
    from base.telemetry.machine import UNKNOWN_MACHINE

    if not process_state["machine_warned"]:
        process_state["machine_warned"] = True
        report_sink_failure(
            "machine identity",
            RuntimeError(
                "no machine name could be read from "
                + (" and ".join(tried) or "any source")
                + f"; rows carry machine={UNKNOWN_MACHINE!r} and the host name in machine_hostname"
            ),
        )
    return UNKNOWN_MACHINE


# ── pipeline state (per-process singleton) ────────────────────────────────────


# meiva:note
# What a process binding promises about its event pipeline. `EventPipeline` is the one
# implementation; it cannot be named here because its loss path imports this module.
class BoundPipeline(Protocol):
    @property
    def stopped(self) -> bool: ...

    def enqueue(self, event: Event) -> None: ...

    def flush(self) -> DrainResult: ...

    def sync(self, timeout: float = 5.0) -> DrainResult: ...

    def stop(self, timeout: float = 5.0) -> DrainResult: ...

    def record_export_loss(self, report: Event) -> None: ...


# meiva:note
# The slots of the per-process binding. Each slot names one fact a process establishes at boot;
# `jsonl_day` is the day stamp of the last mirror prune.
class ProcessState(TypedDict):
    pipeline: BoundPipeline | None
    process: str
    agent_id: int | None
    machine: str | None
    machine_warned: bool
    machine_hostname: str
    home: Path | None
    cluster: str | None
    jsonl_day: str | None


# meiva:note
# The emitter pipeline stays None until explicit process initialization. Dict mutation avoids
# ruff PLW0603 the same way base/telemetry/tracing.py does.
process_state: ProcessState = {
    "pipeline": None,
    "process": "unknown",
    "agent_id": None,
    "machine": None,
    "machine_warned": False,
    "machine_hostname": machine_hostname(),
    "home": None,
    "cluster": None,
    "jsonl_day": None,
}


def process_name() -> str:
    """This process's bound identity (`init_telemetry(process=...)`), the
    bounded dimension every telemetry record carries as `process`. The OTLP
    backend stamps it into the metrics Resource (`service.name=meiva-<process>`)
    so same-named counters from different process kinds cannot collide into
    one Prometheus series."""
    return process_state["process"]


def ambient_agent_id() -> int | None:
    """Default identity established at process startup, if this process owns an agent."""
    return process_state["agent_id"]
