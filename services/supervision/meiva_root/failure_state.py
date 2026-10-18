"""Root unit failure states: the condition behind the `root_unit_failure_state` signal.

The health monitor derives one condition per unit each round — the unit's
intent is running and it sits in an explicit failure state: a recorded
replacement failure, exhausted automatic restart attempts. The
condition is a STATE, so the monitor emits it on every round while it holds;
the observability stack's rules read that stream (the pending period carries
the debounce, silence after the last round resolves it).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.supervision.meiva_root.intent_store import RestartFailure


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
