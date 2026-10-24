# meiva:note
# What bringing the pooler up does, decided from what the shell observed.
#
# Outcomes for a pooler that is already running:
#
# - **Unchanged and fully bound**: a SIGHUP reload; live connections never bounce, and a
#   transient blip on the private network must not hold `meiva start` hostage behind a
#   serving pooler.
# - **Changed ini or userlist**: restarted, never reloaded. A reload keeps a removed user
#   that already authenticated and still admits its new sessions, so only a fresh process
#   revokes it.
# - **Degraded** (answering on loopback but missing the reachable listener): restarted. pgbouncer
#   treats a failed bind on one `listen_addr` entry as a warning and keeps running on the rest,
#   and a reload does not retry a bind that failed at startup (verified on pgbouncer 1.25.2).
#   The restart waits, bounded, for the reachable address first.
# - **Not running**: started fresh.
#
# The wait for the reachable address guards only the paths that (re)start a pooler: a pooler
# born before the private network assigned its address degrades to loopback-only while a
# loopback-only probe reads it as healthy (task #1288, the 2026-08-16 outage).

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "AnnounceChangedRestart",
    "AnnounceDegradedRestart",
    "AwaitReachableBind",
    "BindContext",
    "Launch",
    "PoolerObservation",
    "PoolerStep",
    "Reload",
    "StopRunning",
    "WriteFiles",
    "plan_convergence",
]


# meiva:note
# What the shell read before changing anything.
#
# `public_listener` is only meaningful while `running`: it says the pooler listens on the
# address remote consumers dial, read from the socket table.
@dataclass(frozen=True)
class PoolerObservation:
    running: bool
    files_changed: bool
    public_listener: bool


# meiva:note
# Write the ini and the userlist whose bytes differ from what is on disk.
@dataclass(frozen=True)
class WriteFiles:
    pass


# meiva:note
# SIGHUP the running pooler and prove its admin console still authenticates.
@dataclass(frozen=True)
class Reload:
    pass


# meiva:note
# Why the bring-up waits for the reachable address: a degraded pooler being restarted into a
# healthy double bind, or a pooler about to be born.
class BindContext(Enum):
    RESTART_OF_DEGRADED = "restart-of-degraded"
    FRESH_START = "fresh-start"


# meiva:note
# Wait, bounded, until the reachable address is assigned to a local interface; an address that
# does not appear fails the bring-up with the context's explanation.
@dataclass(frozen=True)
class AwaitReachableBind:
    context: BindContext


# meiva:note
# Say that the configuration or userlist changed and the pooler restarts because of it.
@dataclass(frozen=True)
class AnnounceChangedRestart:
    pass


# meiva:note
# Say that the running pooler lost its reachable listener and the restart repairs it.
@dataclass(frozen=True)
class AnnounceDegradedRestart:
    pass


# meiva:note
# Stop the running pooler with the safe shutdown only. One that will not stop (a client
# holding a transaction) keeps custody and fails the bring-up rather than being killed
# underneath an ordinary start.
@dataclass(frozen=True)
class StopRunning:
    pass


# meiva:note
# Start a fresh pooler and prove it answers on loopback and on the reachable address.
@dataclass(frozen=True)
class Launch:
    pass


PoolerStep = (
    WriteFiles
    | Reload
    | AwaitReachableBind
    | AnnounceChangedRestart
    | AnnounceDegradedRestart
    | StopRunning
    | Launch
)


# meiva:note
# The steps for one bring-up, in order; the shell stops at the first one that fails.
#
# A degraded pooler is restarted whether or not its files changed: the listener is the
# stronger evidence.
def plan_convergence(observation: PoolerObservation) -> tuple[PoolerStep, ...]:
    start = (AwaitReachableBind(BindContext.FRESH_START), Launch())
    if not observation.running:
        return (WriteFiles(), *start)
    if not observation.public_listener:
        return (
            WriteFiles(),
            AwaitReachableBind(BindContext.RESTART_OF_DEGRADED),
            AnnounceDegradedRestart(),
            StopRunning(),
            *start,
        )
    if observation.files_changed:
        return (WriteFiles(), AnnounceChangedRestart(), StopRunning(), *start)
    return (WriteFiles(), Reload())
