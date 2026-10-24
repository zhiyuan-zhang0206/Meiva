"""Rule-based scan that marks ingested content carrying prompt-injection
patterns; pure string matching, no model call. Findings ride along as system
notes on the affected turn — the content itself is returned clean.

A mitigation, not a boundary: a clean result means "no known pattern
matched", not "safe".
"""

from typing import Any, cast

from base.agents.messages import injection_scan
from base.agents.messages.security_finding import SecurityFindingEntry as SecurityFindingEntry
from base.log import logger
from meiva.sdk_surface import process_context
from meiva.sdk_surface.pure.media import finding_plan
from meiva.sdk_surface.pure.media.finding_plan import (
    CarriedFindingsError,
    FindingDelivery,
    Findings,
)

__all_for_meiva__ = [
    "SecurityFindingEntry",
    "is_flagged",
    "scan_content",
]

# The graph-state channel a finding is appended to (`BaseAgentState.security_findings`).
_FINDINGS_CHANNEL = "security_findings"


# meiva:note
# The facade read: this process's scan setting and whether an exec turn is running, as a value.
def current_delivery() -> FindingDelivery:
    from meiva.sdk_surface.settings import agent_setting

    return FindingDelivery(
        enabled=agent_setting("security_scan_enabled"), in_turn=process_context.in_exec_turn()
    )


# meiva:note
# Hand findings to the exec turn's state update, the delta the exec child returns and the exec
# node commits through the channel's reducer. The agent host's after_exec hook turns them into a
# SECURITY system note right behind the exec-result ToolMessage and clears the channel (the
# tool_use -> tool_result adjacency invariant forbids interleaving the note between the AIMessage
# and its ToolMessage). Nothing is kept in this process. `finding_plan.plan_delivery` decides
# what happens to them: a disabled scan records nothing; outside an exec turn there is no state
# update to carry a finding, so it is dropped with a logged warning (the agent host's claim-side
# inbound scan uses `injection_scan.scan_inbound_content`, whose caller owns the finding); an
# update that is no longer a dict is an error.
def deliver(delivery: FindingDelivery, findings: Findings) -> None:
    plan = finding_plan.plan_delivery(delivery, findings)
    for dropped in plan.dropped:
        logger.warning(
            "prompt-injection finding from {} ({}) not delivered: scan_content ran outside an "
            "exec turn, which has no state update to carry it",
            dropped.source,
            ", ".join(dropped.triggers),
        )
    if not plan.append:
        return
    update = process_context.get_state_update()
    if not isinstance(update, dict):
        raise TypeError(
            f"meiva.state_update must stay a dict, got {type(update).__name__} (security finding)"
        )
    state = cast("dict[str, Any]", update)
    state[_FINDINGS_CHANNEL] = [*state.get(_FINDINGS_CHANNEL, []), *plan.append]


# meiva:note
# Deliver the findings a failed unit of work kept and return the failure itself, for the caller
# to raise outside its handler so the error keeps its own cause and context.
def deliver_kept(delivery: FindingDelivery, failed: CarriedFindingsError) -> Exception:
    deliver(delivery, failed.findings)
    return failed.error


def scan_content(content: str, source: str = "unknown") -> str:
    """Return `content` unchanged.

    When a prompt-injection pattern is present, a SECURITY system note follows
    the tool result. Outside an agent turn there is nothing to attach a note to:
    the finding is dropped with a logged warning. Claim-side inbound construction has its
    own scan, whose caller owns the finding. The returned content is always clean — no
    warning is prepended.
    """
    findings = finding_plan.findings_of(content, source)
    if findings:
        deliver(current_delivery(), findings)
    return content


def is_flagged(content: str) -> bool:
    """True when `content` carries injection patterns.

    Findings are delivered as SECURITY system notes (exec-child findings by the
    after_exec hook, inbound findings by the claim node), never as a marker inside the
    content, so this checks the patterns directly: does the content contain
    injection patterns?
    """
    return injection_scan.is_flagged(content)
