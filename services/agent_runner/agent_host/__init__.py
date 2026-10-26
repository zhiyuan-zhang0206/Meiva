# meiva:note
# Shared outcome value for one hosted agent turn.

from __future__ import annotations

from base.api_contracts.agent_contract import TurnFailureReport


# meiva:note
# How one hosted invocation ended, as the settle boundary needs it.
#
#     `crashed` stamps the corpse marker: fatal LLM classes + unclassified
#     exceptions — every path where the turn died without completing work. A
#     no-work park (crash-fresh halted claim, open circuit breaker) ends
#     `turn_idle` with `crashed` False, and the settle deliberately leaves the
#     marker untouched, so the park cannot relabel a corpse healthy.
#
#     `aborted` marks the fatal-abort settlement: the turn's failure was written
#     into the flushed checkpoint (`settle_turn_failure`) before this outcome was
#     returned, so the checkpoint is durable and the settle boundary may
#     reconcile the abort's claimed inbounds. Unclassified exceptions end
#     `crashed` without it — their checkpoint was never settled.
#
#     A hosted failure report is awaited only after its own durable settlement.
class TurnOutcome:
    __slots__ = ("aborted", "crashed", "exited", "failure_report")

    def __init__(
        self,
        *,
        exited: bool,
        crashed: bool,
        aborted: bool = False,
        failure_report: TurnFailureReport | None = None,
    ) -> None:
        self.exited = exited
        self.crashed = crashed
        self.aborted = aborted
        self.failure_report = failure_report
