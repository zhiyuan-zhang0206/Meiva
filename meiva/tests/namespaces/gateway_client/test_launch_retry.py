from typing import Any
from uuid import uuid4

import httpx
import pytest

from meiva import agents, gateway_client
from meiva.tests.fixtures.gateway_wire import GatewayWire
from meiva.tests.fixtures.gateway_wire import bound_wire as bound_wire
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.gateway_wire import wire as wire

PRIOR = uuid4()
ATTEMPT = uuid4()
RETRY_PATH = "/api/keyed/v1/agents/42/retry-launch"


def receipt(**changes: Any) -> dict[str, Any]:
    return {
        "agent_id": 42,
        "prior_attempt_id": str(PRIOR),
        "launch_attempt_id": str(ATTEMPT),
        "accepted_at": "2026-10-09T00:00:00Z",
        "accepted": True,
        "execution_observed": False,
        **changes,
    }


@pytest.fixture
def requests(bound_wire: GatewayWire) -> GatewayWire:
    bound_wire.stub.reply("POST", RETRY_PATH, receipt())
    return bound_wire


def submit(**changes: Any) -> int:
    arguments: dict[str, Any] = {
        "idempotency_key": "retry-intent",
        "expected_prior_attempt_id": PRIOR,
    }
    return agents.retry_launch(42, **(arguments | changes))


def test_sdk_uses_fixed_path_key_and_observed_body(requests: GatewayWire) -> None:
    assert submit() == 42
    assert submit(expected_prior_attempt_id=str(PRIOR)) == 42
    assert len(requests.sent) == 2
    for sent in requests.sent:
        assert sent.path == RETRY_PATH
        assert sent.headers["Idempotency-Key"] == "retry-intent"
        assert sent.headers["Idempotency-Scope"] == "principal-v1"
        assert sent.body == {"expected_prior_attempt_id": str(PRIOR)}


@pytest.mark.parametrize("boundary", [agents.retry_launch, gateway_client.retry_launch])
@pytest.mark.parametrize(
    "changes",
    [
        {"require_idempotency": 1},
        {"require_idempotency": True},
        {"idempotency_key": None},
        {"idempotency_key": ""},
        {"idempotency_key": "x" * 129},
        {"expected_prior_attempt_id": None},
        {"expected_prior_attempt_id": True},
        {"expected_prior_attempt_id": "invalid"},
        {"require_idempotency": False},
    ],
)
def test_invalid_admission_has_no_http(
    requests: GatewayWire, boundary: Any, changes: dict[str, Any]
) -> None:
    args = {
        "idempotency_key": "retry-intent",
        "expected_prior_attempt_id": PRIOR,
    }
    with pytest.raises((TypeError, ValueError)):
        boundary(42, **(args | changes))
    assert requests.sent == []


@pytest.mark.parametrize("agent_id", [True, 0, -1, 2**63])
def test_guarded_client_refuses_invalid_agent_ids(requests: GatewayWire, agent_id: int) -> None:
    with pytest.raises((TypeError, ValueError)):
        gateway_client.retry_launch(
            agent_id,
            idempotency_key="intent",
            expected_prior_attempt_id=PRIOR,
        )
    assert requests.sent == []


@pytest.mark.parametrize("status", [404, 409, 500])
def test_http_failure_never_downgrades_or_retries(requests: GatewayWire, status: int) -> None:
    requests.stub.reply("POST", RETRY_PATH, {"detail": "refused"}, status=status)
    with pytest.raises(httpx.HTTPStatusError):
        submit()
    [sent] = requests.sent
    assert sent.path == RETRY_PATH


def test_response_loss_is_retried_with_the_same_identity(requests: GatewayWire) -> None:
    requests.fail(httpx.ReadTimeout, delivered=True)
    assert submit() == 42
    first, second = requests.sent
    assert first.path == second.path
    assert first.headers == second.headers
    assert first.body == second.body


@pytest.mark.parametrize(
    "bad",
    [
        {"id": 42},
        receipt(agent_id=43),
        receipt(agent_id=True),
        receipt(accepted=1),
        receipt(execution_observed=0),
        receipt(prior_attempt_id=str(uuid4())),
        receipt(launch_attempt_id=str(PRIOR)),
        receipt(launch_attempt_id="invalid"),
        {k: v for k, v in receipt().items() if k != "accepted"},
    ],
)
def test_malformed_acceptance_cannot_report_success(
    requests: GatewayWire, bad: dict[str, Any]
) -> None:
    requests.stub.reply("POST", RETRY_PATH, bad)
    with pytest.raises(ValueError):
        submit()
    assert len(requests.sent) == 1


@pytest.mark.parametrize("boundary", [agents.retry_launch, gateway_client.retry_launch])
@pytest.mark.parametrize(
    "arguments",
    [{}, {"idempotency_key": "intent"}, {"expected_prior_attempt_id": PRIOR}],
)
def test_missing_retry_identity_has_no_http(
    requests: GatewayWire, boundary: Any, arguments: dict[str, Any]
) -> None:
    with pytest.raises(TypeError):
        boundary(42, **arguments)
    assert requests.sent == []


@pytest.mark.parametrize(
    "observed",
    [
        {},
        {"last_launch_attempt_id": None},
        {"last_launch_attempt_id": True},
        {"last_launch_attempt_id": "invalid"},
    ],
)
def test_missing_or_invalid_observation_cannot_authorize_a_retry(
    bound_wire: GatewayWire, observed: dict[str, Any]
) -> None:
    bound_wire.stub.reply("GET", "/api/agents/42", observed)
    with pytest.raises((TypeError, ValueError)):
        agents.get_launch_attempt(42)
    [sent] = bound_wire.sent
    assert sent.method == "GET"
