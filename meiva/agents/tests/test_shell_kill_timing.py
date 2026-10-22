from typing import get_type_hints

import pytest

from base.api_contracts.agent_contract import ShellSessionKillTiming
from meiva.agents import ShellSessionsKill as SdkShellSessionsKill


@pytest.mark.parametrize("timing", list(ShellSessionKillTiming))
def test_sdk_shares_timing_and_preserves_values(timing: ShellSessionKillTiming) -> None:
    assert get_type_hints(SdkShellSessionsKill)["when"] is ShellSessionKillTiming
    sdk = SdkShellSessionsKill(when=timing, killed=[2])
    assert sdk.when is timing


# meiva:note
# Exercises the runtime coercion guard against non-enum raw inputs that static typing rejects.
@pytest.mark.parametrize("when", ["later", "", None])
def test_unknown_raw_timing_is_rejected_by_sdk_boundary(when: object) -> None:
    with pytest.raises(ValueError, match="ShellSessionKillTiming"):
        SdkShellSessionsKill(when=when, killed=[])  # pyright: ignore[reportArgumentType]
