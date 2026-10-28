from __future__ import annotations

from dataclasses import dataclass

from base.lm.harness.events import Usage

# meiva:note
# What a finished turn is and what a session has spent so far: the two values every harness
# adapter builds when a turn settles. Pure, so each adapter's settling decisions can be pure too.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md


def add_cost(left: float | None, right: float | None) -> float | None:
    if left is None and right is None:
        return None
    return (left or 0.0) + (right or 0.0)


@dataclass(frozen=True, slots=True)
class SessionTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float | None = 0.0
    turns: int = 0
    per_turn_usage: bool = True
    cost_known: bool = True

    def with_turn(
        self,
        usage: Usage,
        cost_usd: float | None = None,
        *,
        measured: bool = True,
        cost: float | None = None,
    ) -> SessionTotals:
        turn_cost = cost if cost is not None else cost_usd
        return SessionTotals(
            input_tokens=self.input_tokens + usage.input_tokens,
            output_tokens=self.output_tokens + usage.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + usage.cache_creation_input_tokens
            ),
            cache_read_input_tokens=(self.cache_read_input_tokens + usage.cache_read_input_tokens),
            cost_usd=add_cost(self.cost_usd, turn_cost),
            turns=self.turns + 1,
            per_turn_usage=self.per_turn_usage and measured,
            cost_known=self.cost_known and turn_cost is not None,
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
    per_turn_usage: bool = True
