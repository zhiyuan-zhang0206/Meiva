from __future__ import annotations

from dataclasses import dataclass

from base.lm.harness.base_session import (
    EVENT_STREAM_END,
    BaseHarnessSession,
    InboundQueueFullError,
    SessionClosedError,
    SessionRegistry,
    SessionTotals,
    TurnOutcome,
    TurnTicket,
)
from base.lm.harness.process import ProcessObservation

# meiva:note
# `SessionTotals.per_turn_usage` turns False as soon as one turn's numbers were
# unavailable, so a caller can never read a silently-zeroed total as a measured
# one. `CancelReport.settled` is None when the runtime pid is unobservable — an
# unobservable tree is reported as unknown, never as clean — and `remaining` names
# every process that outlived the sweep so the caller decides instead of assuming.
# meiva:link base/lm/harness/deepseek/docs/deepseek.meiva.okf.md


@dataclass(frozen=True, slots=True)
class CancelReport:
    turn_id: str | None
    cancel_ms: float
    settled: bool | None
    observed: tuple[ProcessObservation, ...]
    remaining: tuple[ProcessObservation, ...]
    waited_ms: float
    agent_pid: int | None
    agent_alive: bool | None


__all__ = [
    "EVENT_STREAM_END",
    "BaseHarnessSession",
    "CancelReport",
    "InboundQueueFullError",
    "SessionClosedError",
    "SessionRegistry",
    "SessionTotals",
    "TurnOutcome",
    "TurnTicket",
]
