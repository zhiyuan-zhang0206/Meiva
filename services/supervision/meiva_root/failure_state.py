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
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from services.supervision.meiva_root.intent_store import CallerFacts, IntentSource, RestartFailure

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

    # meiva:note
    # The unit's intent says running and the supervisor observes no running unit: nothing
    # recorded a failure because nothing replaced it, and no restart was attempted — it is
    # simply not there (task #6345). It ranks below both recorded failure states: a unit that
    # failed is reported as having failed, and only when nothing else explains the absence is
    # the absence itself the finding.
    ABSENT = "absent"


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
    # The supervisor's live observation that no process answers for a unit whose intent says
    # running (task #6345). `restart_failed` records what a replacement did; this records that
    # nothing was there to replace. It must come from facts the supervisor already holds —
    # deriving it by probing again would make the monitor itself the load.
    observed_absent: bool = False
    # meiva:note
    # The intent's provenance and age: which verb set it (`operator` makes a stop an
    # owned decision) and when. An unattended operator stop is reported from these.
    intent_source: IntentSource = IntentSource.SELECTION
    intent_since: float | None = None
    intent_caller: CallerFacts | None = None


# meiva:note
# How long an operator stop may sit unattended before its silence is the finding (#5611).
DEFAULT_OPERATOR_STOP_ALARM_S: Final[float] = 6 * 60 * 60.0


# meiva:note
# An explicit operator stop is policy, not failure: it never counts toward the
# restart budget and never pages while the operator still means it. What it may
# not do is stay silent forever. On 2026-10-23 an im-bridge stop sat at DEBUG for
# 14h14m while every alert behind it silently went nowhere, and the caller that
# asked for it was already unrecoverable (#5611). So: one report per stop record,
# once the record is older than the alarm age, carrying the caller. Nothing here
# restarts anything — the finding is that no owner came back.
def note_operator_stop(
    unit_id: str,
    facts: UnitFailureFacts,
    now: float,
    alarm_s: float,
    verdict: str,
    detail: str,
    already_reported: float | None,
) -> float | None:
    from base.log import logger

    if facts.intent_running or facts.intent_source is not IntentSource.OPERATOR:
        _log.debug(
            "[health] unit %s: %s (%s) — expected (operator stop); no action",
            unit_id,
            verdict,
            detail,
        )
        return already_reported
    since = now if facts.intent_since is None else facts.intent_since
    held_s = max(0.0, now - since)
    if held_s < alarm_s:
        _log.debug(
            "[health] unit %s: %s (%s) — expected (operator stop); no action",
            unit_id,
            verdict,
            detail,
        )
        return already_reported
    if already_reported == since:
        _log.debug(
            "[health] unit %s: still held by an operator stop after %.1fh (already reported)",
            unit_id,
            held_s / 3600.0,
        )
        return already_reported
    logger.warning(
        "[health] unit {unit}: held by an operator stop for {held_h:.1f}h and nobody has "
        "restarted it since — the stop is policy, but no owner came back for it ({caller})",
        event="root_unit_operator_stop_unattended",
        unit=unit_id,
        held_h=held_s / 3600.0,
        caller="unknown" if facts.intent_caller is None else facts.intent_caller.summary(),
        stopped_at=since,
    )
    return since


# meiva:note
# Only running intent can fail; an open breaker outranks a recorded replacement error.
def derive_kind(facts: UnitFailureFacts, *, restart_limit_reached: bool) -> FailureKind | None:
    if not facts.intent_running:
        return None
    if restart_limit_reached:
        return FailureKind.BREAKER_OPEN
    if facts.restart_failed is not None:
        return FailureKind.RESTART_FAILED
    if facts.observed_absent:
        return FailureKind.ABSENT
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
    if kind is FailureKind.ABSENT:
        return detail or "intent says running; no process is running"
    return detail
