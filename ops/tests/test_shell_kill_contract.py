import pytest
from pydantic import ValidationError

from base.api_contracts.agent_contract import ShellKillMode
from ops import cluster_status
from ops.cluster import operations as cluster
from ops.rpc_schemas import ShellInfo, ShellKillResult


@pytest.mark.parametrize("mode", list(ShellKillMode))
def test_rpc_and_operation_use_the_same_owner(
    mode: ShellKillMode,
) -> None:
    assert ShellKillResult.model_fields["mode"].annotation is ShellKillMode
    wire = {"mode": mode.value, "interrupted": mode is ShellKillMode.KILLED, "name": None}
    parsed = ShellKillResult.model_validate(wire)
    assert parsed.mode is mode
    assert parsed.model_dump(mode="json") == wire

    def kill(_agent: int, _session: int) -> tuple[ShellKillMode, bool, str | None]:
        return mode, mode is ShellKillMode.KILLED, None

    assert cluster.shell_kill_op(7, 3, kill_shell_fn=kill).mode is mode
    schema = ShellKillResult.model_json_schema()
    ref = schema["properties"]["mode"]["$ref"]
    assert schema["$defs"][ref.rsplit("/", 1)[1]]["enum"] == ["killed", "absent"]


@pytest.mark.parametrize("raw", ["machine_absent", "later", None])
def test_rpc_rejects_transport_and_unknown_modes(raw: object) -> None:
    with pytest.raises(ValidationError):
        ShellKillResult.model_validate({"mode": raw})


def test_host_producer_returns_the_canonical_absent_member() -> None:
    def no_shells(_agent: int) -> list[ShellInfo]:
        return []

    mode, interrupted, name = cluster_status.kill_shell(7, 3, shell_sessions_fn=no_shells)
    assert mode is ShellKillMode.ABSENT
    assert interrupted is False
    assert name is None


def test_operation_rejects_unknown_host_verdict() -> None:
    def invalid_kill(_agent: int, _session: int) -> tuple[str, bool, str | None]:
        return "machine_absent", False, None

    with pytest.raises(ValueError, match="ShellKillMode"):
        cluster.shell_kill_op(7, 3, kill_shell_fn=invalid_kill)
