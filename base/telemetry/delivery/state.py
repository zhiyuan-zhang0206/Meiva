"""The per-process emitter binding: pipeline, process name, agent, machine and cluster."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Protocol, TypedDict

from base.telemetry.delivery.receipts import DrainResult, Event


def _hostname() -> str:
    return socket.gethostname()


def resolve_machine(name_reader: Callable[[], str] | None = None) -> str:
    """Resolve this event's machine dimension from its explicit owner reader.
    Ordinary diagnostics without a reader use the hostname so a row always
    carries the dimension.

    Only the documented absence (`MachineNameMissing`) falls back silently.
    Any OTHER failure propagates rather than pinning the owner to a wrong
    dimension. An unbound ordinary diagnostic uses the hostname; it never
    discovers a configured machine through a process-global identity reader."""
    from base.cluster.machine import MachineNameMissing

    try:
        return name_reader() if name_reader is not None else _hostname()
    except MachineNameMissing:
        return _hostname()


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
