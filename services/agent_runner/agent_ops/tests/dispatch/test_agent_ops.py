# meiva:note
# The agent-scoped operations against a real pty service and a real agent row.

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from base.sessions.pty import client
from base.sessions.pty.tests.pty_service import service_socket
from base.sessions.pty.tests.pty_shells import new, output_until, type_line, wait_for
from services.agent_runner.agent_ops.tests.ops_support import Ops


async def test_an_agent_without_shells_probes_empty(ops: Ops, agent_id: int) -> None:
    status, result = await ops.dispatch("shell_probe", {"agent_id": agent_id})

    assert (status, result) == ("completed", {"shells": []})


async def test_a_killed_shell_that_is_already_gone_is_an_absent_success(
    ops: Ops, agent_id: int
) -> None:
    status, result = await ops.dispatch("shell_kill", {"agent_id": agent_id, "session_id": 999})

    assert status == "completed"
    assert result == {"mode": "absent", "interrupted": False, "name": None}


async def test_capturing_a_shell_that_died_is_a_plain_failed_result(
    ops: Ops, agent_id: int
) -> None:
    status, result = await ops.dispatch(
        "shell_capture", {"agent_id": agent_id, "session_id": 9, "lines": 20}
    )

    assert status == "failed"
    assert "ShellNotFoundError" in str(result["error"])
    assert "no live shell" in str(result["error"])


@pytest.mark.usefixtures("pty_service")
async def test_a_live_shell_is_probed_captured_and_killed(
    ops: Ops, agent_id: int, tmp_path: Path
) -> None:
    name = f"meiva-agent-{agent_id}-shell-3-build"
    assert new(name, tmp_path)
    type_line(name, "echo shell-output-marker")
    output_until(name, "shell-output-marker")

    probed_status, probed = await ops.dispatch("shell_probe", {"agent_id": agent_id})
    captured_status, captured = await ops.dispatch(
        "shell_capture", {"agent_id": agent_id, "session_id": 3, "lines": 50}
    )
    killed_status, killed = await ops.dispatch(
        "shell_kill", {"agent_id": agent_id, "session_id": 3}
    )

    assert probed_status == captured_status == killed_status == "completed"
    shells = cast("list[dict[str, object]]", probed["shells"])
    assert [(shell["id"], shell["name"]) for shell in shells] == [(3, "build")]
    assert captured["session_name"] == name
    assert "shell-output-marker" in "\n".join(cast("list[str]", captured["lines"]))
    assert killed["mode"] == "killed"
    assert wait_for(lambda: not client.has_session(name, socket_path=service_socket()))


async def test_shell_capture_defaults_to_two_hundred_lines(ops: Ops, agent_id: int) -> None:
    from ops.rpc_schemas import ShellCapturePayload

    assert ShellCapturePayload.model_validate({"agent_id": agent_id, "session_id": 2}).lines == 200


async def test_an_agent_skill_view_lists_commands_and_mcp_names(ops: Ops, agent_id: int) -> None:
    status, result = await ops.dispatch("agent_skill_view", {"agent_id": agent_id})

    assert status == "completed"
    assert isinstance(result["commands"], list)
    names = cast("list[str]", result["mcp_names"])
    assert names == sorted(names)
