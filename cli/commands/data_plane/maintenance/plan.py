# meiva:note
# Which captured data-plane services a maintenance stop ends, in which order, and how.
#
# The pooler goes first so its clients are disconnected before Postgres goes down, Postgres
# second, and Redis last. Every service is identified before the first stop, so the shell
# hands this module the names it captured and executes the steps returned. Redis saves its
# current in-memory data before shutdown; an explicit `save=False` is reserved for callers that
# already verified a final snapshot.

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

__all__ = [
    "ShutdownRedis",
    "StopPooler",
    "StopPostgres",
    "StopStep",
    "fast_shutdown_reserve_s",
    "plan_stop",
]


# meiva:note
# SIGINT the pooler: the safe shutdown disconnects its clients and waits only for in-flight
# server transactions, so neither it nor Postgres waits on idle client connections a paused
# runner may still hold (issue #2307).
@dataclass(frozen=True)
class StopPooler:
    service: ClassVar[str] = "pgbouncer"


# meiva:note
# Postgres' native fast shutdown (disconnect idle sessions, roll back stragglers, checkpoint);
# one that outlives its share of the budget is ended by an immediate shutdown
# (docs/decisions/data/database/2026-10-02-pg-stop-escalates-to-immediate.md).
@dataclass(frozen=True)
class StopPostgres:
    service: ClassVar[str] = "postgres"


# meiva:note
# Redis' own SHUTDOWN with `mode` (SAVE or NOSAVE). redis-py's shutdown helper accepts any
# connection error as success, so the shell sends the command itself and accepts the expected
# EOF only if the exact PID then exits.
@dataclass(frozen=True)
class ShutdownRedis:
    service: ClassVar[str] = "redis"
    mode: str


StopStep = StopPooler | StopPostgres | ShutdownRedis


# meiva:note
# The steps for the services that were captured; a name this stop does not know is a bug in
# the caller and raises.
def plan_stop(captured: frozenset[str], *, save: bool) -> tuple[StopStep, ...]:
    unknown = captured - {StopPooler.service, StopPostgres.service, ShutdownRedis.service}
    if unknown:
        raise ValueError(f"unknown data-plane service: {sorted(unknown)}")
    steps: list[StopStep] = []
    if StopPooler.service in captured:
        steps.append(StopPooler())
    if StopPostgres.service in captured:
        steps.append(StopPostgres())
    if ShutdownRedis.service in captured:
        steps.append(ShutdownRedis("SAVE" if save else "NOSAVE"))
    return tuple(steps)


# meiva:note
# What Postgres' fast shutdown leaves for the legs after it: the immediate shutdown's wait,
# the SIGKILL wait, and the same cleanup wait for Redis' save.
def fast_shutdown_reserve_s(cleanup_s: float, kill_s: float) -> float:
    return cleanup_s + kill_s + cleanup_s
