import pytest

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
from agent.turn.failure_decision import (
    abort_message,
    breaker_already_open,
    circuit_reason,
    provider_recovery,
)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, CIRCUIT_REASON_AUTH),
        (402, CIRCUIT_REASON_BILLING),
        (403, CIRCUIT_REASON_FORBIDDEN),
        (404, CIRCUIT_REASON_MODEL_NOT_FOUND),
        (422, CIRCUIT_REASON_SCHEMA),
        (400, CIRCUIT_REASON_BAD_REQUEST),
        (418, CIRCUIT_REASON_PERMANENT),
        (None, CIRCUIT_REASON_PERMANENT),
    ],
)
def test_a_permanent_rejection_maps_its_status_to_the_required_user_action(
    status: int | None, reason: str
) -> None:
    assert circuit_reason(status=status, context_overflow=False) == reason


def test_a_context_overflow_wins_over_the_status() -> None:
    assert circuit_reason(status=400, context_overflow=True) == CIRCUIT_REASON_CONTEXT_OVERFLOW


@pytest.mark.parametrize(
    ("reason", "fragment"),
    [
        (CIRCUIT_REASON_CONTEXT_OVERFLOW, "Compact the conversation"),
        (CIRCUIT_REASON_AUTH, "credentials or billing"),
        (CIRCUIT_REASON_BILLING, "credentials or billing"),
        (CIRCUIT_REASON_FORBIDDEN, "credentials or billing"),
        (CIRCUIT_REASON_MODEL_NOT_FOUND, "different model overlay, then send"),
        (CIRCUIT_REASON_SCHEMA, "Correct the request"),
        (CIRCUIT_REASON_BAD_REQUEST, "provider policy rejection"),
        (CIRCUIT_REASON_PERMANENT, "provider policy rejection"),
    ],
)
def test_every_reason_names_what_the_user_does_next(reason: str, fragment: str) -> None:
    assert fragment in provider_recovery(reason)


def test_a_blocked_abort_says_heartbeats_will_not_retry_and_an_unblocked_one_says_it_retries() -> (
    None
):
    assert abort_message("FatalProviderError: x", blocked=True).startswith(
        "FatalProviderError: x The agent is blocked; heartbeat check-ins will not re-run"
    )
    assert "retries on the next message or wake-up" in abort_message("E: y", blocked=False)


@pytest.mark.parametrize(
    ("circuit", "expected"),
    [
        (None, False),
        (CircuitState(open=False, reason="billing", opened_at="t"), False),
        (CircuitState(open=True, reason="auth", opened_at="t"), False),
        (CircuitState(open=True, reason="billing", opened_at="t"), True),
    ],
)
def test_the_breaker_is_already_open_only_for_the_same_reason(
    circuit: CircuitState | None, expected: bool
) -> None:
    assert breaker_already_open(circuit, "billing") is expected
