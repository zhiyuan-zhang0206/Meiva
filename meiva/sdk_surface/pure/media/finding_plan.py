# meiva:note
# Prompt-injection findings as values: what a scan of some content yields, and where a finding
# goes once the process facts are known. The callers (`meiva/web`, `meiva/understand`) return the
# findings of their work next to the result; the facade (`meiva/security.py`) reads whether
# scanning is enabled and whether an exec turn is running, asks `plan_delivery` what to do, and
# is the only code that appends to the turn's state update.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from base.agents.messages import injection_scan
from base.agents.messages.security_finding import SecurityFindingEntry

__all__ = [
    "CarriedFindingsError",
    "DeliveryPlan",
    "FindingDelivery",
    "Findings",
    "Scanned",
    "findings_of",
    "gather",
    "model_finding",
    "plan_delivery",
]

type Findings = tuple[SecurityFindingEntry, ...]

# meiva:note
# The trigger label of a finding raised by a model's judgement rather than a pattern.
_MODEL_TRIGGER = "instructions aimed at an AI reader"


# meiva:note
# What a finding needs to reach the agent, read once by the facade: whether scanning is enabled
# and whether an exec turn is running.
@dataclass(frozen=True)
class FindingDelivery:
    enabled: bool
    in_turn: bool

    # meiva:note
    # True when a finding recorded now would reach the agent, so a caller can skip a paid check
    # whose verdict would be dropped.
    @property
    def active(self) -> bool:
        return self.enabled and self.in_turn


# meiva:note
# What to do with findings: `append` goes onto the exec turn's state update, `dropped` has no turn
# to carry it and is reported in the log.
@dataclass(frozen=True)
class DeliveryPlan:
    append: Findings
    dropped: Findings


# meiva:note
# A result with the findings its production raised.
@dataclass(frozen=True)
class Scanned[T]:
    value: T
    findings: Findings = ()


# meiva:note
# A failure that keeps the findings raised before it. The batch shell delivers the findings and
# then raises `error` itself (`meiva.security.deliver_failure`), so a caller never sees this
# wrapper.
class CarriedFindingsError(Exception):
    def __init__(self, error: Exception, findings: Findings) -> None:
        super().__init__(str(error))
        self.error = error
        self.findings = findings


# meiva:note
# The findings of `content` read as `source`: one entry when a pattern matched, none otherwise.
def findings_of(content: str, source: str) -> Findings:
    hits = injection_scan.triggers(content)
    if not hits:
        return ()
    return (SecurityFindingEntry(source=source, triggers=hits),)


# meiva:note
# The finding of a model's judgement that `source`'s content is aimed at an AI reader; `score` is
# the model's probability for that verdict.
def model_finding(source: str, score: float) -> SecurityFindingEntry:
    return SecurityFindingEntry(
        source=source, triggers=[f"{_MODEL_TRIGGER} (model score {score:.2f})"]
    )


# meiva:note
# The results of a batch in input order, with the findings of every item in that order.
def gather[T](parts: Sequence[Scanned[T]]) -> Scanned[list[T]]:
    return Scanned(
        [part.value for part in parts],
        tuple(finding for part in parts for finding in part.findings),
    )


# meiva:note
# A disabled scan records nothing; outside an exec turn there is no state update to carry a
# finding, so it is dropped (and reported); inside one it is appended.
def plan_delivery(delivery: FindingDelivery, findings: Findings) -> DeliveryPlan:
    if not delivery.enabled:
        return DeliveryPlan(append=(), dropped=())
    if not delivery.in_turn:
        return DeliveryPlan(append=(), dropped=findings)
    return DeliveryPlan(append=findings, dropped=())
