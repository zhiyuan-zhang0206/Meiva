# meiva:note
# Destination availability and effective model pins are checked before transfer side effects.
from __future__ import annotations

import pytest
from fastapi import HTTPException

from base.api_contracts.agent_contract import Harness
from gateway.agents.transfer.harness_precheck import validate_transfer_harness
from gateway.schemas.models import HarnessAvailability


def _availability(*, launchable: bool = True) -> HarnessAvailability:
    return HarnessAvailability.model_validate(
        {
            "harness": "codex",
            "status": "available",
            "reason": "probe result",
            "launchable": launchable,
            "billing": "subscription",
            "models": [{"id": "model-a", "reasoning_effort_options": ["high"]}],
        }
    )


@pytest.mark.asyncio
async def test_native_transfer_never_probes_external_harnesses() -> None:
    async def ensure(machine: str) -> list[HarnessAvailability]:
        pytest.fail("native transfer must not probe external CLIs")

    await validate_transfer_harness(ensure, Harness.MEIVA, "target", {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "launchable,model,expected", [(False, "model-a", 409), (True, "missing", 422)]
)
async def test_unavailable_destination_or_unsupported_model_is_refused(
    launchable: bool,
    model: str,
    expected: int,
) -> None:
    seen: list[str] = []

    async def ensure(machine: str) -> list[HarnessAvailability]:
        seen.append(machine)
        return [_availability(launchable=launchable)]

    with pytest.raises(HTTPException) as error:
        await validate_transfer_harness(ensure, Harness.CODEX, "target", {"llm_model": model})
    assert error.value.status_code == expected
    assert seen == ["target"]


@pytest.mark.asyncio
async def test_supported_destination_model_and_effort_pass() -> None:
    async def ensure(machine: str) -> list[HarnessAvailability]:
        return [_availability()]

    await validate_transfer_harness(
        ensure, Harness.CODEX, "target", {"llm_model": "model-a", "reasoning_effort": "high"}
    )
