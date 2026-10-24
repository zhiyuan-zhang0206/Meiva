# meiva:note
# `usage_budget.py` holds the decisions of the usage CLI over values only: how a window timestamp and an agent id are read, the budget reminder a report earns, and what a polling round does next. A reminder leaves the receiving agent in charge of graceful disposition: it states the cost against the limit and names the options, and it terminates nobody and grants no spending. `agent_usage.py` reads the database and the clock, sends the reminder and waits.
# meiva:link meiva_builtins/skills/coordination/meiva-being-a-long-running-agent/docs/meiva-being-a-long-running-agent.meiva.okf.md
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NamedTuple


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("window timestamps must include a timezone")
    return result.astimezone(UTC)


def positive_id(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError("agent IDs must be positive integers")
    return result


# meiva:note
# Refuse a limit that is not positive and finite. `None` means no limit was set.
def check_usd_limit(usd_limit: float | None) -> None:
    if usd_limit is not None and (
        isinstance(usd_limit, bool) or not math.isfinite(usd_limit) or usd_limit <= 0
    ):
        raise ValueError("USD limit must be positive and finite")


# meiva:note
# What a reminder states about a usage report: the recorded cost, the calls without a price, the agents counted, the lineage and the window it covered.
@dataclass(frozen=True)
class BudgetFacts:
    recorded_cost_usd: float
    unpriced_calls: int
    agent_ids: tuple[int, ...]
    lineage: str
    start: str
    end: str


# meiva:note
# The reminder the facts earn against `usd_limit`, or `None` when the recorded cost is under it or no limit was set. A limit that is not positive and finite is refused, whatever the facts say.
def budget_message(facts: BudgetFacts, *, usd_limit: float | None) -> str | None:
    check_usd_limit(usd_limit)
    if usd_limit is None or facts.recorded_cost_usd < usd_limit:
        return None
    return (
        f"Usage budget reminder: recorded USD {facts.recorded_cost_usd:.6f} "
        f">= {usd_limit}. Agents: {list(facts.agent_ids)}; "
        f"lineage: {facts.lineage}; window: [{facts.start}, {facts.end}). "
        f"Unpriced calls: {facts.unpriced_calls}. Reassess before adding work. "
        "Preserve useful results and recovery notes; finish a safe in-flight unit, "
        "prepare a handoff, narrow remaining work, or request a revised budget as appropriate. "
        "This reminder has not terminated any agent or granted additional spending."
    )


class Notify(NamedTuple):
    message: str


class Sleep(NamedTuple):
    seconds: float


class Finish(NamedTuple):
    pass


# meiva:note
# What a round does after reading its report: a reminder is sent once and ends the run; with no reminder, a polling run waits `poll_seconds` and reads again, and a single read ends.
def next_step(message: str | None, poll_seconds: float | None) -> Notify | Sleep | Finish:
    if message is not None:
        return Notify(message)
    if poll_seconds is None:
        return Finish()
    return Sleep(poll_seconds)
