# meiva:note
# The pure decisions of a fatal LLM failure: which circuit reason a permanent provider
# rejection maps to, what the user is told to do, the abort text, and whether an open
# breaker already covers a reason. `runloop.py` reads the checkpoint, runs the effects and
# writes the update.

from __future__ import annotations

from agent.state_channels import (
    CIRCUIT_REASON_AUTH,
    CIRCUIT_REASON_BAD_REQUEST,
    CIRCUIT_REASON_BILLING,
    CIRCUIT_REASON_CONTEXT_OVERFLOW,
    CIRCUIT_REASON_FORBIDDEN,
    CIRCUIT_REASON_MODEL_NOT_FOUND,
    CIRCUIT_REASON_PERMANENT,
    CIRCUIT_REASON_SCHEMA,
    CircuitState,
)

_REASON_BY_STATUS = {
    401: CIRCUIT_REASON_AUTH,
    402: CIRCUIT_REASON_BILLING,
    403: CIRCUIT_REASON_FORBIDDEN,
    404: CIRCUIT_REASON_MODEL_NOT_FOUND,
    422: CIRCUIT_REASON_SCHEMA,
    400: CIRCUIT_REASON_BAD_REQUEST,
}


# meiva:note
# Map a permanent provider rejection to its circuit-breaker reason. Reasons classify the
# required user action; every permanent rejection suppresses background wakes until corrected.
def circuit_reason(*, status: int | None, context_overflow: bool) -> str:
    if context_overflow:
        return CIRCUIT_REASON_CONTEXT_OVERFLOW
    if status is None:
        return CIRCUIT_REASON_PERMANENT
    return _REASON_BY_STATUS.get(status, CIRCUIT_REASON_PERMANENT)


# meiva:note
# The user action for a blocked permanent provider rejection.
def provider_recovery(reason: str) -> str:
    if reason == CIRCUIT_REASON_CONTEXT_OVERFLOW:
        return "Compact the conversation, then send a new message."
    if reason in (CIRCUIT_REASON_AUTH, CIRCUIT_REASON_BILLING, CIRCUIT_REASON_FORBIDDEN):
        return "Resolve the provider credentials or billing issue, then send a new message."
    if reason == CIRCUIT_REASON_MODEL_NOT_FOUND:
        return "Choose a different model overlay, then send a new message."
    if reason == CIRCUIT_REASON_SCHEMA:
        return "Correct the request or model configuration, then send a new message."
    return "Choose a different model overlay or resolve the provider policy rejection, then send a new message."


# meiva:note
# The abort text for the frontend: `failure` names the exception, `blocked` says automatic
# recovery is halted for it.
def abort_message(failure: str, *, blocked: bool) -> str:
    if blocked:
        return (
            f"{failure} The agent is blocked; heartbeat check-ins "
            "will not re-run this request. Resolve the cause, then send a new message."
        )
    return (
        f"{failure} The turn was aborted; the agent "
        "is still alive and idling. It retries on the next message or "
        "wake-up once the underlying cause is resolved."
    )


# meiva:note
# Whether the checkpoint's breaker is already open for the same reason, so a repeated failed
# wake neither reopens it nor repeats the open event. No state at all means not open.
def breaker_already_open(circuit: CircuitState | None, reason: str) -> bool:
    return circuit is not None and circuit.open and circuit.reason == reason
