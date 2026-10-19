from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from base.lm.harness.events import Usage
from base.lm.harness.process import ProcessObservation

# meiva:note
# `SessionTotals.per_turn_usage` turns False as soon as one turn's numbers were
# unavailable, so a caller can never read a silently-zeroed total as a measured
# one. `CancelReport.settled` is None when the runtime pid is unobservable — an
# unobservable tree is reported as unknown, never as clean — and `remaining` names
# every process that outlived the sweep so the caller decides instead of assuming.
# meiva:link base/lm/harness/deepseek/docs/deepseek.meiva.okf.md


@dataclass(frozen=True, slots=True)
class SessionTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    turns: int = 0
    per_turn_usage: bool = True
    cost_usd: float = 0.0
    cost_known: bool = True

    def with_turn(self, usage: Usage, *, measured: bool, cost: float | None) -> SessionTotals:
        return SessionTotals(
            input_tokens=self.input_tokens + usage.input_tokens,
            output_tokens=self.output_tokens + usage.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + usage.cache_read_input_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + usage.cache_creation_input_tokens
            ),
            turns=self.turns + 1,
            per_turn_usage=self.per_turn_usage and measured,
            cost_usd=self.cost_usd + (cost or 0.0),
            cost_known=self.cost_known and cost is not None,
        )


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    turn_id: str
    text: str
    subtype: str
    is_error: bool
    interrupted: bool
    stop_reason: str | None
    usage: Usage
    cost_usd: float | None
    permission_denials: tuple[str, ...]
    per_turn_usage: bool


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


class TurnTicket:
    def __init__(
        self,
        *,
        turn_id: str,
        text: str,
        key: str | None,
        future: asyncio.Future[TurnOutcome],
        deduplicated: bool = False,
    ) -> None:
        self.turn_id = turn_id
        self.text = text
        self.key = key
        self.deduplicated = deduplicated
        self.submitted_at = time.monotonic()
        self.started_at: float | None = None
        self.interrupt_requested = False
        self.parts: list[str] = []
        self._future = future

    async def outcome(self) -> TurnOutcome:
        return await self._future

    @property
    def settled(self) -> bool:
        return self._future.done()

    def settle(self, outcome: TurnOutcome) -> None:
        if not self._future.done():
            self._future.set_result(outcome)

    def fail(self, exc: BaseException) -> None:
        if self._future.done():
            return
        self._future.set_exception(exc)
        self._future.exception()
