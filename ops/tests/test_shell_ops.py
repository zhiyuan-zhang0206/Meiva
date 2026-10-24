# meiva:note
# The shell ops of a runner, against a real pty-sessions service (the `pty_service` fixture) or
# none at all.
#
# The shell-kill verdict has one owner: the RPC model and the host producer share
# `ShellKillMode`, and the operation answers with what the host's session backend did. The probe
# and capture ops answer with the shells of the agent that run on this host.

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from base.api_contracts.agent_contract import ShellKillMode
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from base.sessions.pty.tests.pty_shells import new, output_until, type_line
from ops import cluster_status
from ops.cluster import operations as cluster
from ops.cluster_status import ShellNotFoundError
from ops.rpc_schemas import ShellKillResult


@pytest.mark.parametrize("mode", list(ShellKillMode))
def test_the_rpc_model_uses_the_same_owner_as_the_host(mode: ShellKillMode) -> None:
    assert ShellKillResult.model_fields["mode"].annotation is ShellKillMode
    wire = {"mode": mode.value, "interrupted": mode is ShellKillMode.KILLED, "name": None}
    parsed = ShellKillResult.model_validate(wire)
    assert parsed.mode is mode
    assert parsed.model_dump(mode="json") == wire
    schema = ShellKillResult.model_json_schema()
    ref = schema["properties"]["mode"]["$ref"]
    assert schema["$defs"][ref.rsplit("/", 1)[1]]["enum"] == ["killed", "absent"]


@pytest.mark.parametrize("raw", ["machine_absent", "later", None])
def test_rpc_rejects_transport_and_unknown_modes(raw: object) -> None:
    with pytest.raises(ValidationError):
        ShellKillResult.model_validate({"mode": raw})


@pytest.mark.usefixtures("unit_home")
def test_host_producer_returns_the_canonical_absent_member() -> None:
    mode, interrupted, name = cluster_status.kill_shell(7, 3)

    assert mode is ShellKillMode.ABSENT
    assert interrupted is False
    assert name is None


@pytest.mark.usefixtures("unit_home")
def test_the_operation_reports_an_absent_shell_without_detail() -> None:
    assert cluster.shell_kill_op(7, 3) == ShellKillResult(mode=ShellKillMode.ABSENT)


@pytest.mark.usefixtures("pty_service")
def test_the_operation_reports_the_shell_it_killed(unit_home: Path) -> None:
    name = "meiva-agent-7-shell-3-build"
    new(name, unit_home)
    type_line(name, "echo ready")
    output_until(name, "ready")

    result = cluster.shell_kill_op(7, 3)

    assert result == ShellKillResult(mode=ShellKillMode.KILLED, interrupted=False, name="build")
    assert cluster.shell_kill_op(7, 3).mode is ShellKillMode.ABSENT


@pytest.mark.usefixtures("pty_service")
def test_the_probe_op_lists_the_agents_shells_of_this_host(unit_home: Path) -> None:
    for name in ("meiva-agent-7-shell-0", "meiva-agent-7-shell-1-watcher", "meiva-agent-8-shell-0"):
        new(name, unit_home)

    result = cluster.shell_probe_op(7)

    assert [(shell.id, shell.name) for shell in result.shells] == [(0, None), (1, "watcher")]
    assert all(shell.created_at is not None for shell in result.shells)


@pytest.mark.usefixtures("unit_home")
def test_the_probe_op_of_a_host_without_the_agents_shells_is_empty() -> None:
    assert cluster.shell_probe_op(7).shells == []


@pytest.mark.usefixtures("pty_service")
def test_the_capture_op_returns_the_tail_of_the_resolved_shell(unit_home: Path) -> None:
    name = "meiva-agent-7-shell-2-build"
    new(name, unit_home)
    type_line(name, "echo op-captured-marker")
    output_until(name, "op-captured-marker")

    result = cluster.shell_capture_op(7, 2, 100)

    assert result.session_name == name
    assert "op-captured-marker" in [line.strip() for line in result.lines]
    assert result.created_at is not None


@pytest.mark.usefixtures("unit_home")
def test_the_capture_op_of_a_shell_that_is_not_there_is_not_found() -> None:
    with pytest.raises(ShellNotFoundError):
        cluster.shell_capture_op(7, 2)
