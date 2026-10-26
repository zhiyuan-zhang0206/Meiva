import asyncio

import pytest

from base.agents.sdk import call_policy
from base.agents.sdk import telemetry as sdk_usage_telemetry
from base.agents.sdk.tally import SdkCallTally
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner


@pytest.fixture
def sampling_owner() -> call_policy.SamplingPolicyOwner:
    return fixed_sampling_owner()


def test_simple_emit_unknown_error_is_not_recovered(
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    failure = RuntimeError("emitter programming error")

    def broken() -> str:
        raise failure

    with pytest.raises(RuntimeError) as raised:
        sdk_usage_telemetry.emit(
            "files.write",
            identity={},
            sampling_owner=sampling_owner,
            machine_reader=broken,
        )
    assert raised.value is failure


@pytest.mark.parametrize("async_call", [False, True])
async def test_successful_body_exposes_unknown_emit_failure_without_retry(
    async_call: bool,
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    failure = RuntimeError("emitter programming error")
    effects: list[str] = []
    tally = SdkCallTally()

    def broken() -> str:
        raise failure

    def body() -> str:
        effects.append("committed")
        return "done"

    async def async_body() -> str:
        return body()

    with pytest.raises(RuntimeError) as raised:
        if async_call:
            await sdk_usage_telemetry.run_metered_async(
                "files.write",
                async_body,
                (),
                {},
                identity={},
                tally=tally,
                sampling_owner=sampling_owner,
                machine_reader=broken,
            )
        else:
            sdk_usage_telemetry.run_metered(
                "files.write",
                body,
                (),
                {},
                identity={},
                tally=tally,
                sampling_owner=sampling_owner,
                machine_reader=broken,
            )
    assert raised.value is failure
    assert effects == ["committed"]
    assert tally.snapshot() == {"files.write": 1}


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("primary_type", [ValueError, asyncio.CancelledError, KeyboardInterrupt])
async def test_emit_secondary_failure_keeps_body_identity_cause_and_tally(
    async_call: bool,
    primary_type: type[BaseException],
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    primary = primary_type("body outcome")
    cause = LookupError("original cause")
    secondary = RuntimeError("emitter programming error")
    effects: list[str] = []
    tally = SdkCallTally()

    def broken() -> str:
        raise secondary

    def body() -> None:
        effects.append("committed")
        raise primary from cause

    async def async_body() -> None:
        body()

    with pytest.raises(primary_type) as raised:
        if async_call:
            await sdk_usage_telemetry.run_metered_async(
                "files.write",
                async_body,
                (),
                {},
                identity={},
                tally=tally,
                sampling_owner=sampling_owner,
                machine_reader=broken,
            )
        else:
            sdk_usage_telemetry.run_metered(
                "files.write",
                body,
                (),
                {},
                identity={},
                tally=tally,
                sampling_owner=sampling_owner,
                machine_reader=broken,
            )
    assert raised.value is primary
    assert primary.__cause__ is cause
    assert any(
        "RuntimeError" in note and "emitter programming error" in note for note in primary.__notes__
    )
    assert effects == ["committed"]
    assert tally.snapshot() == {"files.write": 1}
