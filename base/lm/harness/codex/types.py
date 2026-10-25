from __future__ import annotations

from dataclasses import dataclass

from base.lm.harness.events import Usage


@dataclass(frozen=True, slots=True)
class SessionTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0

    def with_turn(self, usage: Usage, cost_usd: float | None) -> SessionTotals:
        return SessionTotals(
            input_tokens=self.input_tokens + usage.input_tokens,
            output_tokens=self.output_tokens + usage.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + usage.cache_creation_input_tokens
            ),
            cache_read_input_tokens=self.cache_read_input_tokens + usage.cache_read_input_tokens,
            cost_usd=self.cost_usd + (cost_usd or 0.0),
            turns=self.turns + 1,
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
    num_turns: int = 1
    steps: tuple[Usage, ...] = ()
