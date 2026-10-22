import pytest
from pydantic import ValidationError

from base.api_contracts.agent_contract import ShellSessionKillTiming
from ops.rpc_schemas.terminate import ShellSessionsKill as RpcShellSessionsKill


@pytest.mark.parametrize("timing", list(ShellSessionKillTiming))
def test_rpc_shares_timing_and_serializes_existing_values(timing: ShellSessionKillTiming) -> None:
    assert RpcShellSessionsKill.model_fields["when"].annotation is ShellSessionKillTiming
    wire = {"when": timing.value, "killed": [2]}
    rpc = RpcShellSessionsKill.model_validate(wire)
    assert rpc.when is timing
    assert rpc.model_dump(mode="json") == wire
    schema = RpcShellSessionsKill.model_json_schema()
    ref = schema["properties"]["when"]["$ref"]
    assert schema["$defs"][ref.rsplit("/", 1)[1]]["enum"] == ["now", "at_exit"]


@pytest.mark.parametrize("when", ["later", "", None])
def test_unknown_raw_timing_is_rejected_by_rpc_boundary(when: object) -> None:
    with pytest.raises(ValidationError):
        RpcShellSessionsKill.model_validate({"when": when, "killed": []})
