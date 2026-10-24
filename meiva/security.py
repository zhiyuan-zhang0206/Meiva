"""Rule-based scan that marks ingested content carrying prompt-injection
patterns; pure string matching, no model call. Findings ride along as system
notes on the affected turn — the content itself is returned clean.

A mitigation, not a boundary: a clean result means "no known pattern
matched", not "safe".
"""

from dataclasses import dataclass
from typing import Any, cast

from base.agents.messages import injection_scan
from base.agents.messages.security_finding import SecurityFindingEntry
from base.log import logger
from meiva.sdk_surface import process_context

__all_for_meiva__ = [
    "SecurityFindingEntry",
    "is_flagged",
    "scan_content",
]

# The graph-state channel a finding is appended to (`BaseAgentState.security_findings`).
_FINDINGS_CHANNEL = "security_findings"

# Trigger label of a finding raised by a model's judgement rather than a pattern.
_MODEL_TRIGGER = "instructions aimed at an AI reader"


# meiva:note
# What a finding needs to reach the agent, read once by the caller: whether scanning is enabled,
# whether an exec turn is running, and that turn's state update (the dict a finding is appended
# to; meaningful only when `in_turn`). The implementation below works on this value only.
@dataclass(frozen=True)
class FindingDelivery:
    enabled: bool
    in_turn: bool
    update: object

    # meiva:note
    # True when a finding recorded now would reach the agent, so a caller can skip a paid check
    # whose verdict would be dropped.
    @property
    def active(self) -> bool:
        return self.enabled and self.in_turn


# meiva:note
# The facade read: this process's scan setting and exec-turn state update, as a value.
def current_delivery() -> FindingDelivery:
    from meiva.sdk_surface.settings import agent_setting

    enabled = agent_setting("security_scan_enabled")
    in_turn = process_context.in_exec_turn()
    return FindingDelivery(
        enabled=enabled,
        in_turn=in_turn,
        update=process_context.get_state_update() if in_turn else None,
    )


# meiva:note
# Append one finding to the exec turn's state update, the delta the exec child returns and the
# exec node commits through the channel's reducer. A disabled scan records nothing; outside an
# exec turn there is no state update to carry the finding, so it is dropped with a logged
# warning; an update that is no longer a dict is an error.
def deliver_finding(delivery: FindingDelivery, source: str, triggers: list[str]) -> None:
    if not delivery.enabled:
        return

    if not delivery.in_turn:
        logger.warning(
            "prompt-injection finding from {} ({}) not delivered: scan_content ran outside an "
            "exec turn, which has no state update to carry it",
            source,
            ", ".join(triggers),
        )
        return
    update = delivery.update
    if not isinstance(update, dict):
        raise TypeError(
            f"meiva.state_update must stay a dict, got {type(update).__name__} (security finding)"
        )
    state = cast("dict[str, Any]", update)
    state[_FINDINGS_CHANNEL] = [
        *state.get(_FINDINGS_CHANNEL, []),
        SecurityFindingEntry(source=source, triggers=triggers),
    ]


# meiva:note
# Deliver the finding of a model's judgement that `source`'s content is aimed at an AI reader;
# `score` is the model's probability for that verdict.
def deliver_model_finding(delivery: FindingDelivery, source: str, score: float) -> None:
    deliver_finding(delivery, source, [f"{_MODEL_TRIGGER} (model score {score:.2f})"])


# meiva:note
# Scan `content` and deliver any finding through `delivery`; the content is returned unchanged.
def scan_to(delivery: FindingDelivery, content: str, source: str) -> str:
    hits = injection_scan.triggers(content)
    if hits:
        deliver_finding(delivery, source, hits)
    return content


def record_finding(source: str, triggers: list[str]) -> None:
    """Hand one security finding to the exec turn's state update.

    The finding is appended to `meiva.state_update["security_findings"]` — the
    delta the exec child returns and the exec node commits through the channel's
    reducer. The agent host's after_exec hook turns it into a SECURITY system
    note right behind the exec-result ToolMessage and clears the channel (the
    tool_use -> tool_result adjacency invariant forbids interleaving the note
    between the AIMessage and its ToolMessage). Nothing is kept in this process.

    No-op when security scanning is disabled. Outside an exec turn no state
    update exists to carry the finding: the finding is dropped, and says so in
    the log (the agent host's claim-side inbound scan uses
    `injection_scan.scan_inbound_content`, whose caller owns the finding).
    """
    deliver_finding(current_delivery(), source, triggers)


def scan_content(content: str, source: str = "unknown") -> str:
    """Return `content` unchanged.

    When a prompt-injection pattern is present, a SECURITY system note follows
    the tool result. Outside an agent turn there is nothing to attach a note to:
    the finding is dropped with a logged warning. Claim-side inbound construction has its
    own scan, whose caller owns the finding. The returned content is always clean — no
    warning is prepended.
    """
    # The finding rides the exec turn's state update (see `record_finding`); the SDK keeps none.
    hits = injection_scan.triggers(content)
    if hits:
        record_finding(source, hits)
    return content


def is_flagged(content: str) -> bool:
    """True when `content` carries injection patterns.

    Findings are delivered as SECURITY system notes (exec-child findings by the
    after_exec hook, inbound findings by the claim node), never as a marker inside the
    content, so this checks the patterns directly: does the content contain
    injection patterns?
    """
    return injection_scan.is_flagged(content)
