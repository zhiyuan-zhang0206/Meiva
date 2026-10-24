from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

import meiva
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.agents.sdk import call_policy
from base.agents.sdk.tally import SdkCallTally
from meiva.sdk_surface import meiva_module, metering, process_context
from meiva.tests.fixtures.pin_agent import pin_agent

pytestmark = pytest.mark.usefixtures("sdk_model_owner")


# meiva:note
# An attachment capture owner whose durable capture fails: the emitter failure the recorder
# must attach to the call's own outcome without retrying the body.
class _CaptureThatFails:
    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    def admit_sdk_call(self) -> "_FailingAdmission":
        return _FailingAdmission(self._failure)

    def capture_local_event(self, event: Any) -> Any:
        raise self._failure


class _FailingAdmission:
    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    def capture(self, event: Any) -> Any:
        raise self._failure

    def capture_failed(self) -> None:
        return None

    def release(self) -> None:
        return None


@pytest.mark.parametrize("committed", [False, True])
def test_installed_send_keeps_unknown_response_when_emission_also_fails(
    unit_home: Path, committed: bool
) -> None:
    emission_failure = RuntimeError("SDK emitter code failed")
    responses: list[httpx.Response] = []

    def reply(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            500,
            request=request,
            json={
                "reason": "unknown_postcommit_failure",
                "detail": "message consumer failed",
                "committed": committed,
                "retryable": False,
                "receipt": {"client_message_id": request.headers["Idempotency-Key"]},
            },
        )
        responses.append(response)
        return response

    transport = GatewayTransportInputs(
        max_retries_reader=lambda: 1,
        retry_delay_reader=lambda: 0.0,
        memory_deadline_reader=lambda: 1.0,
    )
    sampling = call_policy.SamplingPolicyOwner(reader=call_policy.SamplingPolicy)
    with httpx.Client(base_url="http://gateway.test", transport=httpx.MockTransport(reply)) as http:
        clients = ClientSet(
            gateway_url="http://gateway.test",
            gateway=lambda _url: http,
            factories={GatewayTransportInputs: lambda: transport},
        )
        pin_agent(7, owns_loop=False, clients=clients)
        tally = SdkCallTally()
        process_context.bind_context(
            replace(
                meiva.context,
                sdk_calls=tally,
                sdk_capture=_CaptureThatFails(emission_failure),
            )
        )
        ledger = metering.install(
            sampling, clients=None, surface=metering.read_surface(meiva_module())
        )
        try:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                meiva.agents.send_message(42, "one logical send")
        finally:
            metering.uninstall(ledger)
            sampling.close()
    assert len(responses) == 1
    assert raised.value.response is responses[0]
    assert raised.value.__cause__ is None
    assert raised.value.response.json()["receipt"]["client_message_id"]
    assert any("SDK emitter code failed" in note for note in raised.value.__notes__)
    assert tally.snapshot() == {"agents.send_message": 1}
    assert not list((unit_home / "state" / "delivery-outbox").glob("*.json"))
