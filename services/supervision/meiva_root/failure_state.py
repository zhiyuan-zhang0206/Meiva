"""Root unit failure states: the condition behind the `root_unit_failure_state` signal.

The health monitor derives one condition per unit each round — the unit's
intent is running and it sits in an explicit failure state: a recorded
replacement failure, exhausted automatic restart attempts. The
condition is a STATE, so the monitor emits it on every round while it holds;
the observability stack's rules read that stream (the pending period carries
the debounce, silence after the last round resolves it).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from services.supervision.meiva_root.intent_store import RestartFailure

_log = logging.getLogger(__name__)


class FailureKind(StrEnum):
    """The failure states a unit can sit in (derivation order in `derive_kind`)."""

    # meiva:note
    # The unit's automatic-restart circuit breaker is open: its restart budget is
    # spent and only the backoff ladder's capped cadence, or a recovered dependency,
    # still replaces it — an unrelated cause needs an operator. It outranks a
    # recorded replacement failure because this is the state an operator must act
    # on, and the only one whose alert reaches a machine whose database is down
    # (task #5315). Observability rules still accept the historical
    # `restarts_exhausted` value.
    BREAKER_OPEN = "breaker_open"

    RESTART_FAILED = "restart_failed"
    """A replacement failed at its down or up half; the monitor may retry within its finite budget."""


@dataclass(frozen=True, slots=True)
class UnitFailureFacts:
    """The supervisor-side facts of one unit (`Supervisor.unit_failure_facts`)."""

    # meiva:note
    # `last_error` is the unit's recorded exit or spawn error: the evidence its
    # breaker and failure-state signals report (task #5315).
    intent_running: bool
    restart_failed: RestartFailure | None
    last_error: str | None = None


# meiva:note
# Only running intent can fail; an open breaker outranks a recorded replacement error.
def derive_kind(facts: UnitFailureFacts, *, restart_limit_reached: bool) -> FailureKind | None:
    if not facts.intent_running:
        return None
    if restart_limit_reached:
        return FailureKind.BREAKER_OPEN
    if facts.restart_failed is not None:
        return FailureKind.RESTART_FAILED
    return None


def describe(facts: UnitFailureFacts, kind: FailureKind, detail: str = "") -> str:
    """One line of observed evidence for the unit's failure kind."""
    failure = facts.restart_failed
    if kind is FailureKind.RESTART_FAILED and failure is not None:
        return f"replacement failed at its {failure.stage.value} half: {failure.detail}"
    if kind is FailureKind.BREAKER_OPEN:
        if failure is not None:
            return (
                f"replacement failed at its {failure.stage.value} half: {failure.detail}; "
                "circuit breaker open"
            )
        return detail or "automatic restart attempts exhausted; circuit breaker open"
    return detail


# meiva:note
# The one courtesy a restart owes a live takeover: the first attempt of an outage waits
# out a window instead of cutting the session off. A restart mid-session strands an
# attachment the external driver has to rebuild by hand (task #5551).
#
# The watchdog asks the glue "does a live impersonation lease sit on this machine?" and
# gets `(lease_id, holder)` or None. Injecting the answer keeps the monitor out of the
# database: a database outage must not make the watchdog blind.
LeaseProbe = Callable[[], tuple[str, str] | None]


@dataclass(slots=True)
class LeaseGraceState:
    # meiva:note
    # One grace per outage, spent the moment it is considered -- not the moment it is
    # taken. A second look at the same lease must not buy a second window, and a grace
    # that found nobody must not be re-probed on every later round.
    spent: bool = False
    waiting: tuple[str, str] | None = None
    since: float | None = None


# meiva:note
# Only the FIRST attempt of an outage yields: by the second, the probe has failed more
# than once and waiting again is not courtesy but hostage-taking. Nothing here spends a
# restart attempt -- a grace that cost an attempt would shorten the outage budget, which
# is the opposite of letting someone finish their work. Returns the deadline the window
# opened, or None when the restart may proceed now.
def consider_lease(
    state: LeaseGraceState,
    *,
    probe: LeaseProbe | None,
    window_s: float,
    unit_id: str,
    attempt: int,
    now: float,
) -> float | None:
    if state.spent or attempt > 1:
        return None
    if probe is None or window_s <= 0:
        return None
    state.spent = True
    try:
        live = probe()
    except Exception as exc:
        _log.warning("[health] unit %s: lease probe failed (%s); not yielding", unit_id, exc)
        return None
    if live is None:
        return None
    lease_id, holder = live
    state.waiting = live
    state.since = now
    from base.log import logger

    logger.warning(
        "[health] unit {unit}: down, holding the first restart for a live lease "
        "({holder}, lease {lease_id})",
        event="root_health_restart_lease_grace",
        unit=unit_id,
        attempt=attempt,
        lease_id=lease_id,
        holder=holder,
        waited_s=round(window_s, 3),
        outcome="deferred",
    )
    return now + window_s


# meiva:note
# The probe came back inside the window: the restart never happened, and this line is
# the only trace that it was ever going to. `waited_s` is the wait that actually
# happened, not the window that was offered.
def report_lease_recovered(
    state: LeaseGraceState,
    *,
    unit_id: str,
    attempt: int,
    window_s: float,
    now: float,
) -> None:
    if state.waiting is None:
        return
    lease_id, holder = state.waiting
    since = state.since
    waited_s = max(0.0, now - since) if since is not None else window_s
    from base.log import logger

    logger.warning(
        "[health] unit {unit}: answers again inside the lease grace; restart cancelled",
        event="root_health_restart_lease_grace",
        unit=unit_id,
        attempt=attempt,
        lease_id=lease_id,
        holder=holder,
        waited_s=round(waited_s, 3),
        outcome="recovered",
    )


def clear_lease_grace(state: LeaseGraceState) -> None:
    state.spent = False
    state.waiting = None
    state.since = None
