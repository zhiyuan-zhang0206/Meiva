# meiva:note
# What to do next while waiting on the root, its keeper or a service.
#
# Every wait on the application root is a loop that observes, asks one of these functions
# what the observation means and either returns, fails or sleeps. The loops read the clock
# and the sockets; the verdicts are values.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from base.deploy.progress_timeout import CRITICAL_SERVICE_SESSIONS
from base.host.permissions_helper.wire import RootStatus
from base.native_process.root_control.ipc import UnitState
from cli.commands.lifecycle.root_control.snapshot import UnitRow

__all__ = [
    "Expired",
    "KeeperPending",
    "KeeperRefused",
    "KeeperRunning",
    "KeeperVerdict",
    "PollStep",
    "ReadinessRound",
    "Retry",
    "keeper_verdict",
    "observe_readiness",
    "unit_ready",
    "unmet_step",
]

# meiva:note
# The wire form of a probe's positive verdict (`ProbeVerdict.ALIVE.value`).
_ALIVE = "alive"


# meiva:note
# Observe again after `after_s` seconds.
@dataclass(frozen=True)
class Retry:
    after_s: float


# meiva:note
# The deadline passed with the condition still unmet.
@dataclass(frozen=True)
class Expired: ...


PollStep = Retry | Expired


# meiva:note
# The next step of a bounded wait after an observation that did not meet its condition.
#
# A wait observes first and asks this afterwards, so a condition that already holds
# settles even when the budget is spent.
def unmet_step(*, elapsed_s: float, timeout_s: float, interval_s: float) -> PollStep:
    if elapsed_s >= timeout_s:
        return Expired()
    return Retry(interval_s)


# meiva:note
# The keeper reports the root it seeded as running.
@dataclass(frozen=True)
class KeeperRunning: ...


# meiva:note
# The keeper will not bring the root up, and `reason` says why.
@dataclass(frozen=True)
class KeeperRefused:
    reason: str


# meiva:note
# The keeper has not settled yet.
@dataclass(frozen=True)
class KeeperPending: ...


KeeperVerdict = KeeperRunning | KeeperRefused | KeeperPending


# meiva:note
# What one reading of the root keeper means for a start that seeded it.
def keeper_verdict(keeper: RootStatus, run_dir: str) -> KeeperVerdict:
    state = keeper["state"]
    if state == UnitState.RUNNING:
        return KeeperRunning()
    if state == "conflict":
        return KeeperRefused(
            f"another meiva-root owns {run_dir}; the keeper rests in conflict and will not "
            "spawn (resolve the conflict first)"
        )
    if state == "exited":
        return KeeperRefused(
            f"the keeper stopped restarting a root that keeps exiting ({keeper.get('last_exit')})"
        )
    seed_error = keeper.get("seed_error")
    if seed_error:
        return KeeperRefused(f"the root keeper rejected the seed: {seed_error}")
    return KeeperPending()


# meiva:note
# Require a running unit and a positive protocol response.
def unit_ready(unit: UnitRow | None, verdict: str | None) -> bool:
    return unit is not None and unit["state"] == UnitState.RUNNING and verdict == _ALIVE


# meiva:note
# One observation of the requested services (bare unit ids, roster order).
#
# `unready` and `non_critical_unready` split the services that are not ready by tier;
# only the critical tier decides the wait. `newly_ready` are the ready services not seen
# ready before. `sessions_gone` is the early exit: every unready critical unit is
# stopped, and a stopped unit cannot become ready without explicit reconciliation.
@dataclass(frozen=True)
class ReadinessRound:
    unready: tuple[str, ...]
    non_critical_unready: tuple[str, ...]
    newly_ready: tuple[str, ...]
    sessions_gone: bool

    @property
    def settled(self) -> bool:
        return not self.unready or self.sessions_gone


# meiva:note
# A stopped unit cannot become ready without explicit reconciliation.
def _stopped(unit: UnitRow | None) -> bool:
    return unit is not None and unit["state"] == UnitState.STOPPED


# meiva:note
# Classify the requested services from one status reading and fresh probe verdicts.
def observe_readiness(
    requested: Sequence[str],
    units: Mapping[str, UnitRow],
    verdicts: Mapping[str, str],
    recorded: frozenset[str],
) -> ReadinessRound:
    ready = tuple(sid for sid in requested if unit_ready(units.get(sid), verdicts.get(sid)))
    waiting = tuple(sid for sid in requested if sid not in ready)
    critical = tuple(sid for sid in waiting if sid in CRITICAL_SERVICE_SESSIONS)
    return ReadinessRound(
        unready=critical,
        non_critical_unready=tuple(sid for sid in waiting if sid not in critical),
        newly_ready=tuple(sid for sid in ready if sid not in recorded),
        sessions_gone=bool(critical) and all(_stopped(units.get(sid)) for sid in critical),
    )
